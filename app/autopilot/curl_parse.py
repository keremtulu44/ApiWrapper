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

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.upstream.auth import (
    decode_jwt_claims,
    is_token_expired,
    looks_like_jwt,
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


def _smart_join_two_curl_chunks_ex(prev: str, curr: str) -> tuple[str, int | None]:
    """İki cURL parçasını birleştirir ve kelime ortasından birleştiyse sınırı döndürür."""
    p_no_nl = prev.rstrip("\r\n")
    c_no_nl = curr.lstrip("\r\n")
    p_trim = p_no_nl.rstrip(" \t")
    c_trim = c_no_nl.lstrip(" \t")

    if not p_no_nl:
        return c_no_nl, None
    if not c_no_nl:
        return p_no_nl, None

    # 1) Satır sonu devamından (` \`, ` ^`, `` ` ``) bölündüyse yeni satırı geri koy
    #    (JSON gövdesi içindeki kaçışlardan ayırmak için önünde boşluk şartı aranır).
    if re.search(r"[ \t](?:\\|\^|`)$", p_no_nl) or (
        p_no_nl == "curl \\" or p_no_nl == "curl ^"
    ):
        return f"{p_no_nl}\n{c_no_nl}", None
    # 2) Önceki argüman kapanmış (`'` / `"` / `--compressed`) ve yeni bayrak/devam başlıyorsa
    if c_trim.startswith("-") and (
        p_trim.endswith(("'", '"')) or p_trim.endswith("--compressed")
    ):
        return f"{p_no_nl}\n{c_no_nl}", None
    if c_trim.startswith(("\\", "^", "`")) and (
        p_trim.endswith(("'", '"')) or p_trim.endswith("--compressed")
    ):
        return f"{p_no_nl} {c_no_nl}", None
    if p_trim.endswith("--compressed") and c_trim.startswith(
        ("'", '"', "http://", "https://")
    ):
        return f"{p_no_nl} {c_no_nl}", None
    # 3) Bayrak adı (`-H`, `-b`, `--data-raw`, `curl`) ile değeri tam boşluktan bölündüyse
    if _TRAILING_CURL_FLAG_RE.search(p_trim):
        return f"{p_trim} {c_trim}", None
    # 4) Authorization `Bearer <token>` tam boşluktan bölündüyse
    if re.search(r"(?:^|[\s:])bearer$", p_trim, flags=re.IGNORECASE) and re.match(
        r"^[A-Za-z0-9_.-]", c_trim
    ):
        return f"{p_trim} {c_trim}", None
    # 5) Cookie başlığı `; ` noktasından bölünüp aradaki boşluk Telegram tarafından yutulduysa
    if p_no_nl.endswith(";") and _COOKIE_PAIR_START_RE.match(c_no_nl):
        return f"{p_no_nl} {c_no_nl}", None
    # 6) Uzun çerez, JWT veya JSON gövdesinin tam ortasından (4096. karakterden) bölündüyse
    mid_idx = (
        len(p_no_nl)
        if (not p_no_nl[-1].isspace() and not c_no_nl[0].isspace())
        else None
    )
    return f"{p_no_nl}{c_no_nl}", mid_idx


def _smart_join_two_curl_chunks(prev: str, curr: str) -> str:
    """Telegram'ın ~4096 karakter sınırında böldüğü iki cURL parçasını kayıpsız birleştirir."""
    return _smart_join_two_curl_chunks_ex(prev, curr)[0]


_AUTH_CHUNK_NAME_RE = re.compile(r"^(?P<base>.*auth.*)\.(?P<idx>\d+)$", flags=re.IGNORECASE)
_RAW_AUTH_COOKIE_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z0-9_.-]*auth[A-Za-z0-9_.-]*)=(?P<val>[^;\s'\"]*)",
    flags=re.IGNORECASE,
)
_COOKIE_FLAG_COUNT_RE = re.compile(
    r"(?:^|\s)(?:-b|--cookie)(?:=|\s)|(?:^|\s)(?:-H|--header)(?:=|\s+)(?:\$'|['\"\^])*cookie\s*:",
    flags=re.IGNORECASE,
)
_B64URL_REPAIR_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_+/"
)


def _normalize_chunks_artifacts(chunks: list[str]) -> list[str]:
    """Çok parçalı yapıştırmada zengin-metin/Markdown kaçışlarını parça genelinde çözer."""
    combined = "".join(chunks)
    fix_underscore = r"\_" in combined and any(
        hint in combined
        for hint in (
            r"arena\_",
            r"cf\_clearance",
            r"\_\_cf\_bm",
            r"dpl\_",
            r"chat\_input",
            r"user\_country",
        )
    )
    fix_brackets = (r"\[" in combined or r"\]" in combined) and any(
        hint in combined for hint in (r'":\[', r"\}\]", r'\]"', r'\[{"', r'\["')
    )
    fix_amp = "&amp;" in combined and any(
        hint in combined for hint in ("&amp;rum=", "&amp;id=", "&amp;expire=", "&amp;logs=")
    )
    out: list[str] = []
    for ch in chunks:
        txt = (
            ch.replace("\ufeff", "")
            .replace("\u200b", "")
            .replace("\u2060", "")
            .replace("\u200e", "")
            .replace("\u200f", "")
            .replace("\u00a0", " ")
        )
        txt = _MARKDOWN_LINK_RE.sub(lambda m: m.group("label"), txt)
        if fix_underscore:
            txt = txt.replace(r"\_", "_")
        if fix_brackets:
            txt = txt.replace(r"\[", "[").replace(r"\]", "]")
        if fix_amp:
            txt = txt.replace("&amp;", "&")
        out.append(txt)
    return out


def _strict_decode_supabase_b64_session(b64_body: str) -> dict[str, Any] | None:
    """`base64-` gövdesini sıkı UTF-8 + JSON + JWT/refresh bütünlüğüyle çözer."""
    cand = b64_body.strip()
    if len(cand) < 16:
        return None
    padded = cand + "=" * (-len(cand) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=False)
        if any(b < 0x20 or b == 0x7F for b in raw):
            return None
        decoded = raw.decode("utf-8")
        obj = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    at = str(obj.get("access_token") or "").strip()
    if not at or not looks_like_jwt(at):
        return None
    claims = decode_jwt_claims(at)
    if not claims:
        return None
    user_obj = obj.get("user")
    if (
        isinstance(user_obj, dict)
        and user_obj.get("id")
        and claims.get("sub")
        and str(claims["sub"]) != str(user_obj["id"])
    ):
        return None
    if obj.get("expires_at") and claims.get("exp"):
        try:
            if int(claims["exp"]) != int(obj["expires_at"]):
                return None
        except (TypeError, ValueError):
            return None
    return obj


def _supabase_session_consistency_score(sess: dict[str, Any]) -> int:
    """Supabase oturum JSON'undaki tekrarlanan alanların iç tutarlılık puanını hesaplar."""
    raw_user = sess.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    raw_um = user.get("user_metadata")
    um: dict[str, Any] = raw_um if isinstance(raw_um, dict) else {}
    raw_am = user.get("app_metadata")
    am: dict[str, Any] = raw_am if isinstance(raw_am, dict) else {}
    raw_ids = user.get("identities")
    ids: list[Any] = raw_ids if isinstance(raw_ids, list) else []
    id0: dict[str, Any] = ids[0] if ids and isinstance(ids[0], dict) else {}
    raw_idd = id0.get("identity_data")
    idd: dict[str, Any] = raw_idd if isinstance(raw_idd, dict) else {}
    pts = 0
    if user.get("email") and user.get("email") == um.get("email") == idd.get("email"):
        pts += 4
    if (
        um.get("avatar_url")
        and um.get("avatar_url") == idd.get("avatar_url") == um.get("picture")
    ):
        pts += 4
    if um.get("full_name") and um.get("full_name") == idd.get("full_name"):
        pts += 3
    if um.get("name") and um.get("name") == idd.get("name"):
        pts += 3
    if (
        um.get("sub")
        and um.get("sub") == idd.get("sub") == um.get("provider_id") == id0.get("id")
    ):
        pts += 4
    if am.get("id") and am.get("id") == um.get("id"):
        pts += 3
    if user.get("id") and user.get("id") == id0.get("user_id"):
        pts += 3
    providers_list = am.get("providers")
    if (
        am.get("provider")
        and isinstance(providers_list, list)
        and am.get("provider") in providers_list
    ):
        pts += 4
    return pts


def _valid_utf8_printable_b64_prefix(b64_prefix: str) -> bool:
    aligned_len = (len(b64_prefix) // 4) * 4
    if aligned_len <= 0:
        return True
    try:
        raw = base64.b64decode(b64_prefix[:aligned_len], altchars=b"-_", validate=False)
    except (ValueError, binascii.Error):
        return False
    if any(b < 0x20 or b == 0x7F for b in raw):
        return False
    # Sonda kesilmiş olabilecek en fazla 3 baytlık çok-baytlı UTF-8 kuyruğunu tolere et
    for trim in range(4):
        try:
            (raw[:-trim] if trim else raw).decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def _try_repair_eaten_b64_char(curl_text: str, mid_boundaries: list[int]) -> str | None:
    """Android Telegram istemcisinin ~4096 karakter bölmesinde boşluksuz çerez dizisi
    içinde yuttuğu tek Base64 karakterini (`splitPos + 1` hatası) matematiksel JSON +
    JWT/oturum tutarlılığıyla onarır. Kriptografik token belirsizliği varsa tahmin etmez.
    """
    if not mid_boundaries:
        return None

    chunked_spans: dict[str, dict[int, tuple[int, int, str]]] = {}
    single_spans: dict[str, tuple[int, int, str]] = {}
    for m in _RAW_AUTH_COOKIE_SPAN_RE.finditer(curl_text):
        cname = m.group("name")
        cval = m.group("val")
        v_start, v_end = m.start("val"), m.end("val")
        cm = _AUTH_CHUNK_NAME_RE.match(cname)
        if cm:
            base = cm.group("base").lower()
            idx = int(cm.group("idx"))
            chunked_spans.setdefault(base, {})[idx] = (v_start, v_end, cval)
        elif cval and cname.lower() not in single_spans:
            single_spans[cname.lower()] = (v_start, v_end, cval)

    groups: list[list[tuple[int, int, str]]] = []
    for _base, idx_map in chunked_spans.items():
        ordered = sorted(idx_map)
        if ordered == list(range(len(ordered))):
            groups.append([idx_map[i] for i in ordered])
    for s_name, span_item in single_spans.items():
        if s_name not in chunked_spans:
            groups.append([span_item])

    insertions: list[tuple[int, str]] = []
    for spans in groups:
        s0_start, s0_end, s0_val = spans[0]
        prefix_fix: tuple[int, str] | None = None
        if not s0_val.startswith("base64-"):
            # Sınır tam `"base64-"` ön ekinin içine düştüyse kontrol et
            for b_pos in mid_boundaries:
                if s0_start <= b_pos <= min(s0_end, s0_start + 7):
                    rel = b_pos - s0_start
                    for pch in "base64-":
                        cand0 = s0_val[:rel] + pch + s0_val[rel:]
                        if cand0.startswith("base64-"):
                            s0_val = cand0
                            prefix_fix = (b_pos, pch)
                            break
                if prefix_fix is not None:
                    break
        if not s0_val.startswith("base64-"):
            continue

        if prefix_fix is not None:
            raw_b64 = s0_val[7:] + "".join(sp[2] for sp in spans[1:])
            if _strict_decode_supabase_b64_session(raw_b64) is not None:
                insertions.append(prefix_fix)
            continue

        b64_segments: list[tuple[int, int, str]] = [(s0_start + 7, s0_end, s0_val[7:])]
        for sp_start, sp_end, sp_val in spans[1:]:
            b64_segments.append((sp_start, sp_end, sp_val))

        raw_b64 = "".join(seg[2] for seg in b64_segments)
        if _strict_decode_supabase_b64_session(raw_b64) is not None:
            continue

        hits: list[tuple[int, int]] = []
        running = 0
        for seg_start, seg_end, seg_txt in b64_segments:
            for b_pos in mid_boundaries:
                if seg_start <= b_pos <= seg_end:
                    rel_pos = running + (b_pos - seg_start)
                    if (b_pos, rel_pos) not in hits:
                        hits.append((b_pos, rel_pos))
            running += len(seg_txt)
        hits.sort(key=lambda x: x[1])

        if len(hits) == 1:
            b_pos, rel_pos = hits[0]
            passing: list[tuple[int, str, str, str]] = []
            for ch in _B64URL_REPAIR_ALPHABET:
                cand_b64 = raw_b64[:rel_pos] + ch + raw_b64[rel_pos:]
                sess = _strict_decode_supabase_b64_session(cand_b64)
                if sess is not None:
                    passing.append(
                        (
                            _supabase_session_consistency_score(sess),
                            ch,
                            str(sess.get("access_token") or ""),
                            str(sess.get("refresh_token") or ""),
                        )
                    )
            token_pairs = {(at, rt) for (_sc, _ch, at, rt) in passing}
            if passing and len(token_pairs) == 1:
                passing.sort(key=lambda item: item[0], reverse=True)
                insertions.append((b_pos, passing[0][1]))
        elif len(hits) == 2:
            (b_pos1, rel1), (b_pos2, rel2) = hits
            valid_ch1 = [
                ch1
                for ch1 in _B64URL_REPAIR_ALPHABET
                if _valid_utf8_printable_b64_prefix(
                    raw_b64[:rel1] + ch1 + raw_b64[rel1:rel2]
                )
            ]
            passing2: list[tuple[int, str, str, str, str]] = []
            for ch1 in valid_ch1:
                mid_seg = raw_b64[:rel1] + ch1 + raw_b64[rel1:rel2]
                for ch2 in _B64URL_REPAIR_ALPHABET:
                    cand_b64 = mid_seg + ch2 + raw_b64[rel2:]
                    sess = _strict_decode_supabase_b64_session(cand_b64)
                    if sess is not None:
                        passing2.append(
                            (
                                _supabase_session_consistency_score(sess),
                                ch1,
                                ch2,
                                str(sess.get("access_token") or ""),
                                str(sess.get("refresh_token") or ""),
                            )
                        )
            token_pairs2 = {(at, rt) for (_sc, _c1, _c2, at, rt) in passing2}
            if passing2 and len(token_pairs2) == 1:
                passing2.sort(key=lambda item: item[0], reverse=True)
                insertions.append((b_pos1, passing2[0][1]))
                insertions.append((b_pos2, passing2[0][2]))

    if not insertions:
        return None

    repaired = curl_text
    for pos, ch in sorted(insertions, key=lambda x: x[0], reverse=True):
        repaired = repaired[:pos] + ch + repaired[pos:]
    return repaired


def _build_curl_join_candidates(chunks: list[str]) -> list[str]:
    """Ham dilimleme, sınır-duyarlı birleştirme, Telegram 4096 karakter-yutma onarımı
    ve satır/boşluk adaylarını sırayla üretir.
    """
    non_empty = [chunk for chunk in chunks if chunk and chunk.strip("\r\n")]
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return [non_empty[0]]

    norm_chunks = _normalize_chunks_artifacts(non_empty)

    smart = norm_chunks[0]
    smart_boundaries: list[int] = []
    for nxt in norm_chunks[1:]:
        smart, b_idx = _smart_join_two_curl_chunks_ex(smart, nxt)
        if b_idx is not None:
            smart_boundaries.append(b_idx)

    # Telegram istemcisi tam 4096. karakterden ham dilimleme yaptıysa `raw_concat`
    # orijinal panodaki metni bayt-bayt (satır sonları dahil) korur.
    raw_concat = "".join(norm_chunks)
    raw_boundaries: list[int] = []
    running_len = len(norm_chunks[0])
    for idx in range(1, len(norm_chunks)):
        if not norm_chunks[idx - 1][-1].isspace() and not norm_chunks[idx][0].isspace():
            raw_boundaries.append(running_len)
        running_len += len(norm_chunks[idx])

    repaired_smart = _try_repair_eaten_b64_char(smart, smart_boundaries)
    repaired_raw = (
        _try_repair_eaten_b64_char(raw_concat, raw_boundaries)
        if raw_concat != smart
        else None
    )

    direct = "".join(c.strip("\r\n") for c in norm_chunks)
    trimmed_concat = "".join(c.strip(" \t\r\n") for c in norm_chunks)
    with_newlines = "\n".join(c.strip("\r\n") for c in norm_chunks)
    with_spaces = " ".join(c.strip() for c in norm_chunks)

    candidates: list[str] = []
    for cand in (
        raw_concat,
        smart,
        repaired_smart,
        repaired_raw,
        direct,
        trimmed_concat,
        with_newlines,
        with_spaces,
    ):
        if cand and cand not in candidates:
            candidates.append(cand)
    return candidates


def inspect_agent_cookie_health(cookie: str, access_token: str = "") -> dict[str, Any]:
    """Gizli değer (çerez/token içeriği) döndürmeden Agent oturum çerezi sağlığını denetler."""
    cookies = parse_cookie_header(cookie)
    names = list(cookies.keys())
    lowered_names = [n.lower() for n in names]

    auth_cookie_found = bool(access_token.strip()) or any(
        any(hint in n for hint in ("auth", "session", "token", "jwt"))
        and n not in {"csrftoken", "xsrf-token"}
        and bool(cookies.get(orig_n, "").strip())
        for orig_n, n in zip(names, lowered_names, strict=False)
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
    # Birleştirilmiş Supabase oturum JSON'unda hem tam JSON bütünlüğünü hem de
    # `refresh_token` varlığını (değerini değil!) doğrula; `.0` + `.1` parçalarından
    # biri eksik ya da 4096 kesiminde bozulmuşsa sıkı base64/UTF-8/JSON çözümü reddeder:
    from app.upstream.auth import merge_chunked_cookies

    merged_cookies = merge_chunked_cookies(cookies)
    has_refresh_token = False
    for cname, cval in merged_cookies.items():
        if _AUTH_CHUNK_NAME_RE.match(cname):
            # Ham `.0` / `.1` yarı parçalarını atla; yalnız birleştirilmiş tam tabanı denetle
            continue
        if "auth" in cname.lower() and cval.startswith("base64-"):
            sess_obj = _strict_decode_supabase_b64_session(cval[len("base64-") :])
            if sess_obj is None:
                incomplete_auth_chunks = True
                continue
            if str(sess_obj.get("refresh_token") or "").strip():
                has_refresh_token = True

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
            else ", devam parçası (.1) eksik veya bozuk"
        )
        problems.append(
            f"Parçalı oturum çerezi eksik/bozuk kopyalanmış (görülen parçalar: "
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

    Önce ham dilimlemeyi ve sınır-duyarlı akıllı birleştirmeyi üretir, ardından
    alternatiflerle birlikte `parse_agent_curl` + `inspect_agent_cookie_health`
    üzerinden puanlayarak JSON gövdesi, parçalı çerez (`.0` + `.1`), başlık sayısı
    ve `recaptchaV3Token` bütünlüğünü en iyi koruyan adayı döndürür.
    """
    candidates = _build_curl_join_candidates(chunks)
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    best_cand = candidates[0]
    best_score: tuple[int, int, int, int, int, int, int, int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        try:
            raw_parsed = parse_raw_agent_curl(cand)
            cfg = parse_agent_curl(cand, chat_id_override=chat_id_override)
        except ValueError:
            score = (0, 0, 0, 0, 0, 0, 0, 0, 0, -idx)
        else:
            health = inspect_agent_cookie_health(cfg.cookie, cfg.access_token)
            header_count = len(raw_parsed.get("headers") or {})
            score = (
                1,
                int(not health["incomplete_auth_chunks"]),
                int(health["has_refresh_token"]),
                int(bool(cfg.account_identity())),
                int(health["cookie_count"]),
                -len(cfg.warnings),
                int(bool(cfg.recaptcha_token)),
                int(bool(cfg.last_user_message_id)),
                header_count,
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
    """Windows CMD (`^"`), Android/Chromium ANSI-C (`$'...'`), `'\\''`, tek satıra
    düzleştirilmiş `\\ -H` (Render env kutusu) ve çoklu `-H 'cookie:'` bayraklarını çözer.

    Standart tek çerezli Bash cURL'lerine hiç dokunmaz; yalnızca `app.upstream.curl_import`
    modülünün doğrudan çözemediği biçimler bulunduğunda kayıpsız standart biçime dönüştürür.
    """
    if not raw:
        return raw
    cleaned = _clean_pasted_artifacts(raw)
    has_multi_cookie_flags = len(_COOKIE_FLAG_COUNT_RE.findall(cleaned)) > 1
    has_flattened_continuations = bool(re.search(r"[ \t]\\[ \t]*(?=-)", cleaned))
    needs_rewrite = (
        is_windows_cmd_curl(cleaned)
        or "$'" in cleaned
        or r"'\''" in cleaned
        or has_multi_cookie_flags
        or has_flattened_continuations
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
    destekler. Her aday `parse_curl` + `build_settings` + parçalı çerez, JSON gövde ve
    JWT kimlik kontrolünden geçirilerek en eksiksiz sonuç seçilir.
    """
    raw_candidates = _build_curl_join_candidates(chunks)
    if not raw_candidates:
        return ""
    candidates = [normalize_windows_cmd_curl_for_upstream(c) for c in raw_candidates]
    if len(candidates) == 1:
        return candidates[0]

    best_cand = candidates[0]
    best_score: tuple[int, int, int, int, int, int, int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        try:
            parsed = parse_curl(cand)
            env, warnings, _model_id = build_settings(parsed)
        except Exception:
            score = (0, 0, 0, 0, 0, 0, 0, 0, -idx)
        else:
            critical = any(w.startswith("KRİTİK") for w in warnings)
            has_url = bool(env.get("TARGET_DOMAIN") and env.get("UPSTREAM_STREAM_PATH"))
            cookie = env.get("UPSTREAM_COOKIE", "")
            token = env.get("UPSTREAM_ACCESS_TOKEN", "")
            has_auth = bool(cookie or token)
            valid = int(has_url and has_auth and not critical)
            health = inspect_agent_cookie_health(cookie, token)
            body_ok = int(bool(load_body(str(parsed.get("body") or ""))))
            has_captcha = int(bool(env.get("RECAPTCHA_STATIC_TOKEN")))
            has_sub = int(bool(extract_identity_subject(cookie, token)))
            score = (
                valid,
                int(not health["incomplete_auth_chunks"]),
                int(health["has_refresh_token"]),
                body_ok,
                has_captcha,
                has_sub,
                int(health["cookie_count"]),
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
