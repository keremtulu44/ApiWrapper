"""Agent Mode cURL ayrıştırıcısı (`max` cURL ayrıştırıcısından türetilmiş).

Hem Bash (`Copy as cURL (bash)`) hem de Windows CMD (`Copy as cURL (cmd)`)
biçimlerindeki cURL komutlarını kayıpsız ayrıştırır. Özellikle Windows CMD'nin
ürettiği iki katmanlı kaçışları (``^"``, ``^{``, ``^|``, ``^&``, ``^%``, ``^\\^"``)
tek geçişte çözerek JSON gövdesinin ve parçalı oturum çerezlerinin bozulmasını
engeller.

Desteklenen Agent Mode cURL kaynakları:
1. ``POST /api/chat/{chat_id}/input`` (önerilen: hem ``chat_id`` hem ``metadata``
   şablonunu, ``recaptchaV3Token`` ve ``cx`` değerlerini taşır).
2. ``POST /nextjs-api/stream/create-chat`` (sohbet başlatma isteği; ``chat_id``
   dışarıdan verilirse devam eden sohbete bağlanabilir).
3. ``GET /api/chat/{chat_id}/events`` veya ``GET /api/chat/{chat_id}?recoverRun=false``
   (``chat_id`` ve oturum çerezi içerir; referer'dan da ``chat_id`` çıkarabilir).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.upstream.auth import (
    decode_jwt_claims,
    is_token_expired,
    parse_cookie_header,
    resolve_bearer_token,
    token_seconds_remaining,
)
from app.upstream.curl_import import (
    PASSTHROUGH_HEADERS,
    TELEMETRY_HOSTS,
    TELEMETRY_PATH_PREFIXES,
    build_settings,
    extract_authorization,
    load_body,
    parse_curl,
)

#: UUID (v4/v7) yakalayıcı.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: `/api/chat/{uuid}` veya `/agent/{uuid}` yollarından sohbet kimliği çıkarma.
_AGENT_CHAT_PATH_RE = re.compile(
    r"/(?:api/chat|agent)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$|\?)"
)

#: Markdown bağlantısı olarak yapıştırılmış URL'leri temizler: `[https://x](https://x)`
_MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>https?://[^\]\s]+)\]\((?P<href>https?://[^)\s]+)\)")


def _clean_pasted_artifacts(raw: str) -> str:
    """Mobil pano (Android/Lemur/Gboard), BOM ve Markdown kusurlarını düzeltir."""
    # Görünmez sıfır-genişlikli pano karakterlerini ve UTF-8 BOM'u temizle
    text = (
        raw.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u2060", "")
        .replace("\u200e", "")
        .replace("\u200f", "")
    )
    # Mobil klavye/panoların girintilerde ürettiği kırılmaz boşlukları (NBSP) normalleştir
    if "\u00a0" in text:
        text = text.replace("\u00a0", " ")
    text = _MARKDOWN_LINK_RE.sub(lambda m: m.group("label"), text)
    # Bazı zengin metin editörleri / not uygulamaları alt çizgiyi `\_`, köşeli
    # parantezi `\[` / `\]` ve `&` karakterini `&amp;` olarak kaçırır:
    if r"\_" in text and any(
        hint in text
        for hint in (
            r"arena\_",
            r"cf\_clearance",
            r"\_\_cf\_bm",
            r"dpl\_",
            r"chat\_input",
            r"user\_country",
        )
    ):
        text = text.replace(r"\_", "_")
    if (r"\[" in text or r"\]" in text) and any(
        hint in text for hint in (r'":\[', r"\}\]", r'\]"', r'\[{"', r'\["')
    ):
        text = text.replace(r"\[", "[").replace(r"\]", "]")
    if "&amp;" in text and any(
        hint in text for hint in ("&amp;rum=", "&amp;id=", "&amp;expire=", "&amp;logs=")
    ):
        text = text.replace("&amp;", "&")
    return text


def is_windows_cmd_curl(raw: str) -> bool:
    """Metnin Windows `Copy as cURL (cmd)` çıktısı olup olmadığını belirler."""
    stripped = raw.strip()
    return bool(re.search(r'(?:--url|-H|-b|--cookie|--data-raw|--data|-d)\s+\^"', stripped)) or (
        stripped.startswith('curl ^"') or ' ^\n' in stripped or ' ^\r\n' in stripped
    )


def unescape_windows_cmd(raw: str) -> str:
    """Windows CMD `^` kaçış katmanını tek geçişte çözer.

    CMD'de satır sonundaki ``^`` satır birleştirir; satır içindeki ``^X`` ise
    (``^^`` -> ``^``, ``^"`` -> ``"``, ``^\\`` -> ``\\``, ``^|`` -> ``|`` dahil)
    doğrudan ``X`` karakterini üretir.
    """
    # 1) Satır devamı: `^` + opsiyonel boşluk + yeni satır -> tek boşluk
    text = re.sub(r"\^[ \t]*\r?\n[ \t]*", " ", raw.strip())
    # 2) Tek geçişli `^X` -> `X` çözümü (`^^` da doğru biçimde tek `^` olur)
    return re.sub(r"\^(.)", r"\1", text)


def _unescape_c_argv_double_quotes(value: str) -> str:
    """C runtime çift tırnak içi kaçışlarını (`\\"` -> `"`, `\\\\` -> `\\`) çözer."""
    out: list[str] = []
    idx = 0
    length = len(value)
    while idx < length:
        ch = value[idx]
        if ch == "\\" and idx + 1 < length:
            nxt = value[idx + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                idx += 2
                continue
        out.append(ch)
        idx += 1
    return "".join(out)


def _tokenize_curl(raw: str) -> list[str]:
    """Bash veya CMD cURL metnini sıralı argüman listesine dönüştürür."""
    cleaned = _clean_pasted_artifacts(raw)
    cmd_mode = is_windows_cmd_curl(cleaned)
    if cmd_mode:
        text = unescape_windows_cmd(cleaned)
    else:
        # Bash / PowerShell satır sonu devamlarını birleştir
        text = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", cleaned.strip())
        text = re.sub(r"`[ \t]*\r?\n[ \t]*", " ", text)

    tokens: list[str] = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break

        if cmd_mode:
            if text[idx] == '"':
                idx += 1
                buf: list[str] = []
                while idx < length:
                    ch = text[idx]
                    if ch == "\\" and idx + 1 < length:
                        nxt = text[idx + 1]
                        if nxt in ('"', "\\"):
                            buf.append(nxt)
                            idx += 2
                            continue
                    if ch == '"':
                        idx += 1
                        break
                    buf.append(ch)
                    idx += 1
                tokens.append("".join(buf))
                continue

            start = idx
            while idx < length and not text[idx].isspace():
                idx += 1
            tokens.append(text[start:idx])
            continue

        # POSIX / Bash modu: boşluksuz bitişik tırnak segmentlerini
        # (`'abc'\''def'`, `'abc'"'"'def'`, `'abc'$'\''def'`, `--cookie='...'`)
        # gerçek kabukta olduğu gibi tek argümanda birleştir.
        word_parts: list[str] = []
        while idx < length and not text[idx].isspace():
            if text[idx : idx + 2] == "$'":
                idx += 2
                ansi_buf: list[str] = []
                while idx < length and text[idx] != "'":
                    if text[idx] == "\\" and idx + 1 < length:
                        esc = text[idx + 1]
                        mapping = {"n": "\n", "r": "\r", "t": "\t", "'": "'", "\\": "\\"}
                        if esc in mapping:
                            ansi_buf.append(mapping[esc])
                            idx += 2
                            continue
                        if esc == "u" and idx + 6 <= length:
                            try:
                                ansi_buf.append(chr(int(text[idx + 2 : idx + 6], 16)))
                                idx += 6
                                continue
                            except ValueError:
                                pass
                        if esc == "x" and idx + 4 <= length:
                            try:
                                ansi_buf.append(chr(int(text[idx + 2 : idx + 4], 16)))
                                idx += 4
                                continue
                            except ValueError:
                                pass
                    ansi_buf.append(text[idx])
                    idx += 1
                if idx < length and text[idx] == "'":
                    idx += 1
                word_parts.append("".join(ansi_buf))
                continue

            if text[idx] == "'":
                idx += 1
                start = idx
                while idx < length and text[idx] != "'":
                    idx += 1
                word_parts.append(text[start:idx])
                if idx < length and text[idx] == "'":
                    idx += 1
                continue

            if text[idx] == '"':
                idx += 1
                dq_buf: list[str] = []
                while idx < length:
                    ch = text[idx]
                    if ch == "\\" and idx + 1 < length:
                        nxt = text[idx + 1]
                        if nxt in ('"', "\\", "$", "`"):
                            dq_buf.append(nxt)
                            idx += 2
                            continue
                    if ch == '"':
                        idx += 1
                        break
                    dq_buf.append(ch)
                    idx += 1
                word_parts.append("".join(dq_buf))
                continue

            if text[idx] == "\\":
                if idx + 1 < length:
                    word_parts.append(text[idx + 1])
                    idx += 2
                else:
                    word_parts.append("\\")
                    idx += 1
                continue

            start = idx
            while (
                idx < length
                and not text[idx].isspace()
                and text[idx] not in ("'", '"', "\\")
                and text[idx : idx + 2] != "$'"
            ):
                idx += 1
            word_parts.append(text[start:idx])

        tokens.append("".join(word_parts))

    return tokens


def _append_cookie_fragment(cookie_parts: list[str], raw_val: str) -> None:
    cleaned = raw_val.strip().strip(";").strip()
    if cleaned and cleaned not in cookie_parts:
        cookie_parts.append(cleaned)


def parse_raw_agent_curl(raw: str) -> dict[str, Any]:
    """Ham cURL metninden URL, HTTP metodu, başlıklar ve gövdeyi ayıklar.

    HTTP/2 ve HTTP/3 DevTools çıktılarında birden fazla `-H 'cookie: ...'` veya
    `-b` bayrağı gelebilir; önceki çerezleri ezmek yerine sırayla `; ` ile birleştirir.
    """
    tokens = _tokenize_curl(raw)
    url = ""
    method = ""
    headers: dict[str, str] = {}
    cookie_parts: list[str] = []
    body = ""

    def _record_header(raw_header: str) -> None:
        if raw_header.startswith(":"):
            # HTTP/2 sözde başlıkları (`:authority`, `:path`, `:method`, `:scheme`)
            rest = raw_header[1:]
            pseudo_name, pseudo_sep, pseudo_val = rest.partition(":")
            if pseudo_sep and pseudo_name.strip().lower() == "method" and not method:
                nonlocal_method[0] = pseudo_val.strip().upper()
            return
        name, sep, val = raw_header.partition(":")
        if not sep:
            return
        lower_name = name.strip().lower()
        if not lower_name:
            return
        if lower_name == "cookie":
            _append_cookie_fragment(cookie_parts, val)
        else:
            headers[lower_name] = val.strip()

    nonlocal_method = [""]
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("curl", "curl.exe"):
            idx += 1
            continue
        if tok in ("-H", "--header") and idx + 1 < len(tokens):
            _record_header(tokens[idx + 1])
            idx += 2
            continue
        if tok.startswith("--header="):
            _record_header(tok.split("=", 1)[1])
            idx += 1
            continue
        if tok in ("-b", "--cookie") and idx + 1 < len(tokens):
            _append_cookie_fragment(cookie_parts, tokens[idx + 1])
            idx += 2
            continue
        if tok.startswith("--cookie="):
            _append_cookie_fragment(cookie_parts, tok.split("=", 1)[1])
            idx += 1
            continue
        if tok in ("--data-raw", "--data", "-d", "--data-binary") and idx + 1 < len(tokens):
            body = tokens[idx + 1]
            idx += 2
            continue
        if tok.startswith(("--data-raw=", "--data=", "--data-binary=")):
            body = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok == "--url" and idx + 1 < len(tokens):
            url = tokens[idx + 1].strip().rstrip("^")
            idx += 2
            continue
        if tok.startswith("--url="):
            url = tok.split("=", 1)[1].strip().rstrip("^")
            idx += 1
            continue
        if tok in ("-X", "--request") and idx + 1 < len(tokens):
            method = tokens[idx + 1].strip().upper()
            idx += 2
            continue
        if tok.startswith("--request="):
            method = tok.split("=", 1)[1].strip().upper()
            idx += 1
            continue
        if tok.startswith(("http://", "https://")) and not url:
            url = tok.strip().rstrip("^")
            idx += 1
            continue
        idx += 1

    if cookie_parts:
        headers["cookie"] = "; ".join(cookie_parts)
    if not method and nonlocal_method[0]:
        method = nonlocal_method[0]
    if not method:
        method = "POST" if body else "GET"

    return {
        "url": url,
        "method": method,
        "headers": headers,
        "body": body,
    }


DEFAULT_AGENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
DEFAULT_AGENT_ACCEPT_LANGUAGE = "tr,en;q=0.9,en-GB;q=0.8,en-US;q=0.7"


@dataclass(slots=True)
class AgentCurlConfig:
    """Agent Mode cURL'ünden çıkarılan tam oturum ve şablon yapılandırması."""

    target_domain: str
    scheme: str = "https"
    chat_id: str = ""
    trigger_address: str = ""
    last_user_message_id: str = ""
    last_event_id: str = ""
    cookie: str = ""
    access_token: str = ""
    auth_scheme: str = "Bearer"
    user_agent: str = DEFAULT_AGENT_USER_AGENT
    accept_language: str = DEFAULT_AGENT_ACCEPT_LANGUAGE
    sec_ch_ua_override: str = ""
    sec_ch_ua_platform_override: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    metadata_template: dict[str, Any] = field(default_factory=dict)
    recaptcha_token: str = ""
    cx: str = ""
    timezone: str = "Europe/Istanbul"
    client_platform: str = "web"
    arena_visit_id: str = ""
    submission_source: str = "chat_input"
    source_endpoint_kind: str = "unknown"
    warnings: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.target_domain}"

    @property
    def referer_url(self) -> str:
        if self.chat_id:
            return f"{self.base_url}/agent/{self.chat_id}"
        return f"{self.base_url}/agent"

    @property
    def input_url(self) -> str:
        if not self.chat_id:
            raise ValueError("Agent chat_id bilinmiyor; '/api/chat/{id}/input' kurulamaz.")
        return f"{self.base_url}/api/chat/{self.chat_id}/input"

    @property
    def events_url(self) -> str:
        if not self.chat_id:
            raise ValueError("Agent chat_id bilinmiyor; '/api/chat/{id}/events' kurulamaz.")
        return f"{self.base_url}/api/chat/{self.chat_id}/events"

    def state_url(self, *, recover_run: bool = False) -> str:
        if not self.chat_id:
            raise ValueError("Agent chat_id bilinmiyor; '/api/chat/{id}' kurulamaz.")
        flag = "true" if recover_run else "false"
        return f"{self.base_url}/api/chat/{self.chat_id}?recoverRun={flag}"

    @property
    def effective_trigger_address(self) -> str:
        if self.trigger_address:
            return self.trigger_address
        if self.chat_id:
            return f"agent-runner:{self.chat_id}"
        return ""

    def account_identity(self) -> str:
        """Bu oturumun bağlı olduğu Google/kullanıcı hesabının kararlı JWT `sub` değeri."""
        return extract_identity_subject(self.cookie, self.access_token)


def extract_identity_subject(cookie: str, access_token: str = "") -> str:
    """Çerez veya access token içinden JWT `sub` (kullanıcı kimliği) değerini döndürür."""
    token = resolve_bearer_token(explicit_token=access_token, cookie_header=cookie)
    if not token:
        return ""
    claims = decode_jwt_claims(token)
    sub = claims.get("sub") if isinstance(claims, dict) else None
    return str(sub).strip() if sub else ""


def extract_max_slot_identities(settings: Settings) -> dict[int, str]:
    """Yapılandırılmış `max` hesap yuvalarının (`slot -> JWT sub`) eşlemesini döndürür."""
    by_slot: dict[int, str] = {}
    for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
        suffix = "" if slot <= 1 else f"_{slot}"
        cookie = str(getattr(settings, f"upstream_cookie{suffix}", "") or "").strip()
        access_token = str(
            getattr(settings, f"upstream_access_token{suffix}", "") or ""
        ).strip()
        if not cookie and not access_token:
            continue
        sub = extract_identity_subject(cookie, access_token)
        if sub:
            by_slot[slot] = sub
    return by_slot


def extract_all_max_identities(settings: Settings) -> frozenset[str]:
    """`Settings` içindeki tüm `max` hesap yuvalarının (`1..MAX_ACCOUNT_SLOTS`) JWT `sub` kümesi."""
    return frozenset(extract_max_slot_identities(settings).values())


def detect_pasted_curl_kind(raw: str) -> Literal["agent", "max", "unknown"]:
    """Yapıştırılan cURL metninin Agent Mode mu yoksa Max sohbeti mi olduğunu saptar."""
    if not raw:
        return "unknown"
    lowered = _clean_pasted_artifacts(raw).lower()
    if "create-evaluation" in lowered or "post-to-evaluation" in lowered:
        return "max"
    if (
        "/api/chat/" in lowered
        or "/agent/" in lowered
        or "nextjs-api/stream/create-chat" in lowered
        or "/stream/create-chat" in lowered
    ):
        return "agent"
    return "unknown"


def looks_like_agent_curl_start(raw: str) -> bool:
    """Metnin yapıştırılmış bir `curl ...` komutunun başlangıcı olup olmadığını saptar.

    Normal sohbet cümlelerini (ör. ``"curl nedir?"``) yanlış pozitif saymaz; gerçek
    bir URL veya HTTP bayrağı içeren `curl` komutlarını yakalar ki çerez/token
    yanlışlıkla `max` sohbetine gönderilmesin.
    """
    if not raw:
        return False
    cleaned = _clean_pasted_artifacts(raw).lstrip()
    if not re.match(r"^curl(?:\.exe)?[\s\"'^]", cleaned, flags=re.IGNORECASE):
        return False
    return any(
        marker in cleaned
        for marker in ("https://", "http://", "--url", " -H ", " --header ")
    )


_TRAILING_CURL_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-H|-b|-d|-X|--header|--cookie|--data-raw|--data|--data-binary|--url|curl(?:\.exe)?)$",
    flags=re.IGNORECASE,
)
_COOKIE_PAIR_START_RE = re.compile(r"^[A-Za-z0-9_.-]+=")


def _smart_join_two_curl_chunks(prev: str, curr: str) -> str:
    """Telegram'ın ~4096 karakter sınırında böldüğü iki cURL parçasını kayıpsız birleştirir."""
    p_no_nl = prev.rstrip("\r\n")
    c_no_nl = curr.lstrip("\r\n")
    p_trim = p_no_nl.rstrip(" \t")
    c_trim = c_no_nl.lstrip(" \t")

    if not p_no_nl:
        return c_no_nl
    if not c_no_nl:
        return p_no_nl

    # 1) Satır sonu devamından (` \`, ` ^`, `` ` ``) bölündüyse yeni satırı geri koy
    #    (JSON gövdesi içindeki kaçışlardan ayırmak için önünde boşluk şartı aranır).
    if re.search(r"[ \t](?:\\|\^|`)$", p_no_nl) or (
        p_no_nl == "curl \\" or p_no_nl == "curl ^"
    ):
        return f"{p_no_nl}\n{c_no_nl}"
    # 2) Önceki argüman kapanmış (`'` / `"` / `--compressed`) ve yeni bayrak/URL başlıyorsa
    if c_trim.startswith("-") and (
        p_trim.endswith(("'", '"')) or p_trim.endswith("--compressed")
    ):
        return f"{p_no_nl}\n{c_no_nl}"
    if p_trim.endswith("--compressed") and c_trim.startswith(
        ("'", '"', "http://", "https://")
    ):
        return f"{p_no_nl} {c_no_nl}"
    # 3) Bayrak adı (`-H`, `-b`, `--data-raw`, `curl`) ile değeri tam boşluktan bölündüyse
    if _TRAILING_CURL_FLAG_RE.search(p_trim):
        return f"{p_trim} {c_trim}"
    # 4) Authorization `Bearer <token>` tam boşluktan bölündüyse
    if re.search(r"(?:^|[\s:])bearer$", p_trim, flags=re.IGNORECASE) and re.match(
        r"^[A-Za-z0-9_.-]", c_trim
    ):
        return f"{p_trim} {c_trim}"
    # 5) Cookie başlığı `; ` noktasından bölünüp aradaki boşluk Telegram tarafından yutulduysa
    if p_no_nl.endswith(";") and _COOKIE_PAIR_START_RE.match(c_no_nl):
        return f"{p_no_nl} {c_no_nl}"
    # 6) Uzun çerez, JWT veya JSON gövdesinin tam ortasından (4096. karakterden) bölündüyse
    return f"{p_no_nl}{c_no_nl}"


def _build_curl_join_candidates(chunks: list[str]) -> list[str]:
    """Ham dilimleme, sınır-duyarlı birleştirme ve satır/boşluk adaylarını sırayla üretir."""
    non_empty = [chunk for chunk in chunks if chunk and chunk.strip("\r\n")]
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return [non_empty[0]]

    smart = non_empty[0]
    for nxt in non_empty[1:]:
        smart = _smart_join_two_curl_chunks(smart, nxt)

    # Telegram istemcisi tam 4096. karakterden ham dilimleme yaptıysa `raw_concat`
    # orijinal panodaki metni bayt-bayt (satır sonları dahil) korur.
    raw_concat = "".join(non_empty)
    direct = "".join(c.strip("\r\n") for c in non_empty)
    with_newlines = "\n".join(c.strip("\r\n") for c in non_empty)
    with_spaces = " ".join(c.strip() for c in non_empty)

    candidates: list[str] = []
    for cand in (smart, raw_concat, direct, with_newlines, with_spaces):
        if cand and cand not in candidates:
            candidates.append(cand)
    return candidates


_AUTH_CHUNK_NAME_RE = re.compile(r"^(?P<base>.*auth.*)\.(?P<idx>\d+)$", flags=re.IGNORECASE)


def inspect_agent_cookie_health(cookie: str, access_token: str = "") -> dict[str, Any]:
    """Gizli değer (çerez/token içeriği) döndürmeden Agent oturum çerezi sağlığını denetler."""
    cookies = parse_cookie_header(cookie)
    names = list(cookies.keys())
    lowered_names = [n.lower() for n in names]

    auth_cookie_found = bool(access_token.strip()) or any(
        any(hint in n for hint in ("auth", "session", "token", "jwt"))
        and n not in {"csrftoken", "xsrf-token"}
        for n in lowered_names
    )

    chunk_groups: dict[str, list[int]] = {}
    for name in names:
        m = _AUTH_CHUNK_NAME_RE.match(name)
        if m:
            chunk_groups.setdefault(m.group("base"), []).append(int(m.group("idx")))

    auth_chunk_indices: list[int] = []
    missing_chunk_indices: list[int] = []
    incomplete_auth_chunks = False
    for _base, indices in chunk_groups.items():
        ordered = sorted(set(indices))
        auth_chunk_indices = ordered
        expected = list(range(ordered[-1] + 1))
        if ordered != expected:
            incomplete_auth_chunks = True
            missing_chunk_indices = [i for i in expected if i not in ordered]

    token = resolve_bearer_token(explicit_token=access_token, cookie_header=cookie)
    claims = decode_jwt_claims(token) if token else {}
    jwt_found = bool(claims)
    # Birleştirilmiş Supabase oturum JSON'unda `refresh_token` varlığını (değerini değil!)
    # kontrol et; `refresh_token` varsa sayfa ısıtmasıyla `Set-Cookie` rotasyonu alınabilir.
    from app.upstream.auth import _try_b64_decode, merge_chunked_cookies

    merged_cookies = merge_chunked_cookies(cookies)
    has_refresh_token = False
    for cname, cval in merged_cookies.items():
        if "auth" in cname.lower() and cval.startswith("base64-"):
            decoded = _try_b64_decode(cval[len("base64-") :])
            if decoded and '"refresh_token"' in decoded:
                try:
                    sess_obj = json.loads(decoded)
                    if isinstance(sess_obj, dict) and bool(
                        str(sess_obj.get("refresh_token") or "").strip()
                    ):
                        has_refresh_token = True
                except ValueError:
                    pass

    # Yalnızca parçalı `.0` gelip `.1` eksik kaldığında base64 JSON yarım kalır;
    # `_salvage_truncated_b64` JWT'yi kurtarsa bile çerezin kendisi eksik parçalıdır:
    for base, indices in chunk_groups.items():
        if sorted(set(indices)) == [0]:
            val0 = cookies.get(f"{base}.0", "")
            # ~3000+ karakterlik tek `.0` parçası genelde 4 KB sınırında bölünmüş demektir
            if (
                len(val0) >= 2800
                and val0.startswith("base64-")
                and _try_b64_decode(val0[len("base64-") :]) is None
            ):
                incomplete_auth_chunks = True

    rem_raw = token_seconds_remaining(token) if token else None
    rem_sec = int(round(rem_raw)) if rem_raw is not None else None
    expired = bool(token and is_token_expired(token, leeway=0.0))

    problems: list[str] = []
    if not auth_cookie_found:
        problems.append(
            f"cURL içinde oturum çerezi ('*auth*') görünmüyor (gelen {len(names)} çerez "
            "yalnızca telemetri/Cloudflare olabilir; cURL eksik kopyalanmış veya giriş "
            "yapılmamış olabilir)."
        )
    if incomplete_auth_chunks:
        missing_txt = (
            f", eksik parçalar: {missing_chunk_indices}"
            if missing_chunk_indices
            else ", devam parçası (.1) eksik"
        )
        problems.append(
            f"Parçalı oturum çerezi eksik kopyalanmış (görülen parçalar: "
            f"{auth_chunk_indices}{missing_txt}). "
            "Hedef buna HTTP 401 döner; cURL'ü yeniden eksiksiz kopyalayın."
        )
    if expired and rem_sec is not None:
        mins_ago = max(1, abs(rem_sec) // 60)
        problems.append(
            f"Kopyalanan cURL içindeki erişim token'ının (JWT) süresi {mins_ago} dk önce "
            "dolmuş görünüyor (tarayıcı Set-Cookie ile yeni çereze geçmiş olabilir). "
            "Agent sohbetinde kısa bir mesaj daha gönderip SON '/input' isteğinin cURL'ünü "
            "alın ve sekmeyi hemen kapatın."
        )

    return {
        "cookie_count": len(names),
        "auth_cookie_found": auth_cookie_found,
        "auth_chunk_indices": auth_chunk_indices,
        "incomplete_auth_chunks": incomplete_auth_chunks,
        "jwt_found": jwt_found,
        "has_refresh_token": has_refresh_token,
        "token_expired": expired,
        "token_remaining_seconds": rem_sec,
        "problems": problems,
    }


def reassemble_agent_curl_chunks(
    chunks: list[str],
    *,
    chat_id_override: str | None = None,
) -> str:
    """Telegram gibi mesaj boyutu sınırlı kanallardan gelen çok parçalı Agent cURL'ünü birleştirir.

    Önce sınır-duyarlı akıllı birleştirmeyi ve ham birleştirmeyi üretir, ardından
    alternatiflerle birlikte `parse_agent_curl` üzerinden puanlayarak JSON gövdesi,
    çerez ve `recaptchaV3Token` bütünlüğünü en iyi koruyan adayı döndürür.
    """
    candidates = _build_curl_join_candidates(chunks)
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    best_cand = candidates[0]
    best_score: tuple[int, int, int, int, int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        try:
            cfg = parse_agent_curl(cand, chat_id_override=chat_id_override)
        except ValueError:
            score = (0, 0, 0, 0, 0, 0, -idx)
        else:
            health = inspect_agent_cookie_health(cfg.cookie, cfg.access_token)
            score = (
                1,
                int(not health["incomplete_auth_chunks"]),
                int(bool(cfg.account_identity())),
                -len(cfg.warnings),
                int(bool(cfg.recaptcha_token)),
                int(bool(cfg.last_user_message_id)),
                -idx,
            )
        if best_score is None or score > best_score:
            best_score = score
            best_cand = cand

    return best_cand


def _quote_for_upstream_curl_import(value: str) -> str:
    """`app.upstream.curl_import._split_top_level` ile %100 uyumlu çift tırnaklama."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def normalize_windows_cmd_curl_for_upstream(raw: str) -> str:
    """Windows CMD (`^"`), Android/Chromium ANSI-C (`$'...'`) ve `'\\''` kaçışlarını çözer.

    Standart Bash cURL'lerine hiç dokunmaz; yalnızca `app.upstream.curl_import`
    modülünün doğrudan çözemediği CMD `^"`, Bash `$'...'` / `'...\\''...'` veya
    mobil pano görünmez karakterleri bulunduğunda kayıpsız standart biçime dönüştürür.
    """
    if not raw:
        return raw
    cleaned = _clean_pasted_artifacts(raw)
    needs_rewrite = (
        is_windows_cmd_curl(cleaned)
        or "$'" in cleaned
        or r"'\''" in cleaned
    )
    if not needs_rewrite:
        # Standart Bash biçimindeyse ama `curl_import.parse_curl` gövdeyi okuyamadıysa
        # yine de tam ayrıştırıcıyla kurtarmayı dene:
        try:
            quick = parse_curl(cleaned)
            quick_body = str(quick.get("body") or "").strip()
            if quick_body and not load_body(quick_body):
                needs_rewrite = True
        except Exception:
            needs_rewrite = True

    if not needs_rewrite:
        return cleaned

    parsed = parse_raw_agent_curl(cleaned)
    url = str(parsed.get("url") or "").strip()
    headers: dict[str, str] = parsed.get("headers") or {}
    body = str(parsed.get("body") or "")
    if not url:
        return cleaned

    parts = [f"curl {_quote_for_upstream_curl_import(url)}"]
    for name, val in headers.items():
        if name == "cookie":
            parts.append(f"-b {_quote_for_upstream_curl_import(val)}")
        else:
            parts.append(f"-H {_quote_for_upstream_curl_import(f'{name}: {val}')}")
    if body:
        parts.append(f"--data-raw {_quote_for_upstream_curl_import(body)}")
    return " \\\n  ".join(parts)


def reassemble_upstream_curl_chunks(chunks: list[str]) -> str:
    """Telegram'ın böldüğü çok parçalı `max` (`create-evaluation`) cURL'ünü kayıpsız birleştirir.

    Hem Bash (`Copy as cURL (bash)`) hem Windows CMD (`Copy as cURL (cmd)`) girdilerini
    destekler. Her aday `parse_curl` + `build_settings` + JSON gövde ve JWT kimlik
    kontrolünden geçirilerek en eksiksiz sonuç seçilir.
    """
    raw_candidates = _build_curl_join_candidates(chunks)
    if not raw_candidates:
        return ""
    candidates = [normalize_windows_cmd_curl_for_upstream(c) for c in raw_candidates]
    if len(candidates) == 1:
        return candidates[0]

    best_cand = candidates[0]
    best_score: tuple[int, int, int, int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        try:
            parsed = parse_curl(cand)
            env, warnings, _model_id = build_settings(parsed)
        except Exception:
            score = (0, 0, 0, 0, 0, -idx)
        else:
            critical = any(w.startswith("KRİTİK") for w in warnings)
            has_url = bool(env.get("TARGET_DOMAIN") and env.get("UPSTREAM_STREAM_PATH"))
            cookie = env.get("UPSTREAM_COOKIE", "")
            token = env.get("UPSTREAM_ACCESS_TOKEN", "")
            has_auth = bool(cookie or token)
            valid = int(has_url and has_auth and not critical)
            body_ok = int(bool(load_body(str(parsed.get("body") or ""))))
            has_captcha = int(bool(env.get("RECAPTCHA_STATIC_TOKEN")))
            has_sub = int(bool(extract_identity_subject(cookie, token)))
            score = (
                valid,
                body_ok,
                has_captcha,
                has_sub,
                -len(warnings),
                -idx,
            )
        if best_score is None or score > best_score:
            best_score = score
            best_cand = cand

    return best_cand


def _extract_chat_id_from_text(text: str) -> str:
    if not text:
        return ""
    match = _AGENT_CHAT_PATH_RE.search(text)
    return match.group(1) if match else ""


def parse_agent_curl(
    raw: str,
    *,
    chat_id_override: str | None = None,
) -> AgentCurlConfig:
    """Agent Mode cURL metnini ayrıştırıp doğrulanmış `AgentCurlConfig` döndürür.

    Gizli değerler (cookie/token) hata metinlerine asla eklenmez.
    """
    if not raw or not raw.strip():
        raise ValueError("Agent cURL metni boş.")

    trimmed_head = _clean_pasted_artifacts(raw).lstrip()
    if re.match(r"^(?:Invoke-WebRequest|iwr|fetch\s*\()", trimmed_head, flags=re.IGNORECASE):
        raise ValueError(
            "Bu metin PowerShell/fetch biçiminde kopyalanmış. Tarayıcı Network "
            "sekmesinde sağ tıklayıp 'Copy -> Copy as cURL (bash)' (veya Copy as "
            "cURL (cmd)) seçeneğini kullanın."
        )

    parsed = parse_raw_agent_curl(raw)
    url = parsed["url"]
    headers: dict[str, str] = parsed["headers"]
    raw_body: str = parsed["body"]

    if not url:
        raise ValueError("Agent cURL içinden hedef URL çıkarılamadı.")

    split = urlsplit(url)
    host = split.netloc.strip()
    scheme = (split.scheme or "https").strip().lower()
    if not host:
        raise ValueError("Agent cURL içinden alan adı (domain) çıkarılamadı.")

    lowered_host = host.lower()
    lowered_path = (split.path or "").lower()
    if any(marker in lowered_host for marker in TELEMETRY_HOSTS) or any(
        lowered_path.startswith(prefix) for prefix in TELEMETRY_PATH_PREFIXES
    ):
        raise ValueError(
            "Verilen Agent cURL'ü bir telemetri/analitik isteğine (rum/logs/posthog) ait. "
            "Network sekmesinde '/api/chat/<id>/input' isteğini kopyalayın."
        )

    cookie = headers.get("cookie", "").strip()
    auth = extract_authorization(headers)
    access_token = auth[1] if auth else ""
    auth_scheme = auth[0] if auth else "Bearer"

    if not cookie and not access_token:
        raise ValueError(
            "Agent cURL içinde oturum çerezi (-b / cookie) veya authorization başlığı yok."
        )

    warnings: list[str] = []
    body_obj: dict[str, Any] = {}
    if raw_body.strip():
        try:
            loaded = json.loads(raw_body)
            if isinstance(loaded, dict):
                body_obj = loaded
            else:
                warnings.append("cURL gövdesi JSON nesnesi değil; şablon alanları boş kaldı.")
        except json.JSONDecodeError:
            warnings.append(
                "cURL gövdesi geçerli JSON olarak okunamadı; Copy as cURL (bash) tercih edin."
            )

    # Endpoint türünü ve chat_id'yi belirle
    source_kind = "unknown"
    if "/input" in lowered_path and "/api/chat/" in lowered_path:
        source_kind = "input"
    elif "/events" in lowered_path and "/api/chat/" in lowered_path:
        source_kind = "events"
    elif "/api/chat/" in lowered_path:
        source_kind = "state"
    elif "create-chat" in lowered_path:
        source_kind = "create-chat"
    elif "create-evaluation" in lowered_path or "post-to-evaluation" in lowered_path:
        raise ValueError(
            "Bu cURL normal sohbet moduna (create-evaluation) ait görünüyor; "
            "Agent Mode sohbetinden ('/api/chat/<id>/input') alınmış cURL gerekli."
        )

    chat_id = (chat_id_override or "").strip()
    if not chat_id:
        chat_id = _extract_chat_id_from_text(split.path)
    if not chat_id:
        chat_id = _extract_chat_id_from_text(headers.get("referer", ""))

    trigger_address = ""
    last_user_message_id = ""
    metadata_template: dict[str, Any] = {}
    recaptcha_token = ""
    cx = ""
    timezone = "Europe/Istanbul"
    client_platform = "web"
    arena_visit_id = ""
    submission_source = "chat_input"

    # 1) `/api/chat/{id}/input` gövde şeması: `{"kind": "message", "payload": {...}}`
    payload = body_obj.get("payload")
    if isinstance(payload, dict):
        raw_chat_id_field = str(payload.get("chatId") or "").strip()
        if raw_chat_id_field:
            trigger_address = raw_chat_id_field
            if not chat_id and ":" in raw_chat_id_field:
                candidate = raw_chat_id_field.split(":", 1)[1].strip()
                if _UUID_RE.fullmatch(candidate):
                    chat_id = candidate
            elif not chat_id and _UUID_RE.fullmatch(raw_chat_id_field):
                chat_id = raw_chat_id_field

        msg_node = payload.get("message")
        if isinstance(msg_node, dict):
            msg_meta = msg_node.get("metadata")
            if isinstance(msg_meta, dict):
                last_user_message_id = str(msg_meta.get("lastUserMessageId") or "").strip()

        meta = payload.get("metadata")
        if isinstance(meta, dict):
            metadata_template = dict(meta)
            recaptcha_token = str(meta.get("recaptchaV3Token") or "").strip()
            cx = str(meta.get("cx") or "").strip()
            timezone = str(meta.get("timezone") or timezone).strip()
            client_platform = str(meta.get("clientPlatform") or client_platform).strip()
            arena_visit_id = str(meta.get("arenaVisitId") or "").strip()
            submission_source = str(meta.get("submissionSource") or submission_source).strip()

    # 2) `create-chat` gövde şeması yedeği
    raw_root_id = body_obj.get("id")
    if not chat_id and isinstance(raw_root_id, str) and _UUID_RE.fullmatch(raw_root_id.strip()):
        chat_id = raw_root_id.strip()
    if not recaptcha_token and isinstance(body_obj.get("recaptchaV3Token"), str):
        recaptcha_token = body_obj["recaptchaV3Token"].strip()
    if not cx and isinstance(body_obj.get("cx"), str):
        cx = body_obj["cx"].strip()
    if isinstance(body_obj.get("timezone"), str) and body_obj["timezone"].strip():
        timezone = body_obj["timezone"].strip()
    if isinstance(body_obj.get("clientPlatform"), str) and body_obj["clientPlatform"].strip():
        client_platform = body_obj["clientPlatform"].strip()
    if not last_user_message_id and isinstance(body_obj.get("message"), dict):
        first_msg_id = body_obj["message"].get("id")
        if isinstance(first_msg_id, str) and first_msg_id.strip():
            last_user_message_id = first_msg_id.strip()

    # Çerezden `arena_visit_id` yedeği (ör. `/events` cURL'ü verildiyse)
    if not arena_visit_id and "arena_visit_id=" in cookie:
        visit_match = _UUID_RE.search(cookie.split("arena_visit_id=", 1)[1][:160])
        if visit_match:
            arena_visit_id = visit_match.group(0)

    if not chat_id:
        raise ValueError(
            "Agent cURL içinden sohbet kimliği (chat_id) çıkarılamadı. "
            "Sohbet başlatıldıktan sonraki 2. mesajın ('/api/chat/<id>/input') cURL'ünü "
            "kullanın veya chat_id değerini ayrıca belirtin."
        )

    if not recaptcha_token:
        warnings.append(
            "cURL gövdesinde 'recaptchaV3Token' bulunamadı ('/input' yerine '/events' veya "
            "durum isteği kopyalanmış olabilir). Hedef captcha doğruluyorsa '/input' "
            "isteğinin cURL'ünü kullanın."
        )

    cookie_health = inspect_agent_cookie_health(cookie, access_token)
    if cookie_health["incomplete_auth_chunks"] or cookie_health["token_expired"]:
        for problem in cookie_health["problems"]:
            if problem not in warnings:
                warnings.append(problem)

    extra_headers: dict[str, str] = {}
    for name in PASSTHROUGH_HEADERS:
        val = headers.get(name, "").strip()
        if val:
            extra_headers[name] = val
    # Android/mobil tarayıcıdan (ör. Lemur Browser) alınan cURL'lerde `sec-ch-ua-mobile: ?1`
    # başlığını koru ki User-Agent + `cf_clearance` parmak iziyle çelişmesin:
    if headers.get("sec-ch-ua-mobile", "").strip() == "?1":
        extra_headers["sec-ch-ua-mobile"] = "?1"

    last_event_id = headers.get("last-event-id", "").strip()

    return AgentCurlConfig(
        target_domain=host,
        scheme=scheme,
        chat_id=chat_id,
        trigger_address=trigger_address or f"agent-runner:{chat_id}",
        last_user_message_id=last_user_message_id,
        last_event_id=last_event_id,
        cookie=cookie,
        access_token=access_token,
        auth_scheme=auth_scheme or "Bearer",
        user_agent=headers.get("user-agent", "").strip()
        or DEFAULT_AGENT_USER_AGENT,
        accept_language=headers.get("accept-language", "").strip()
        or DEFAULT_AGENT_ACCEPT_LANGUAGE,
        sec_ch_ua_override=headers.get("sec-ch-ua", "").strip(),
        sec_ch_ua_platform_override=headers.get("sec-ch-ua-platform", "").strip(),
        extra_headers=extra_headers,
        metadata_template=metadata_template,
        recaptcha_token=recaptcha_token,
        cx=cx,
        timezone=timezone,
        client_platform=client_platform,
        arena_visit_id=arena_visit_id,
        submission_source=submission_source,
        source_endpoint_kind=source_kind,
        warnings=warnings,
    )
