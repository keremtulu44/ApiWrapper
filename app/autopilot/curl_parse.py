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
from typing import Any
from urllib.parse import urlsplit

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.upstream.auth import decode_jwt_claims, resolve_bearer_token
from app.upstream.curl_import import (
    PASSTHROUGH_HEADERS,
    TELEMETRY_HOSTS,
    TELEMETRY_PATH_PREFIXES,
    extract_authorization,
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
    """Sohbet/Markdown editörlerinden ve Windows UTF-8 BOM'dan gelen kusurları düzeltir."""
    text = raw.lstrip("\ufeff")
    text = _MARKDOWN_LINK_RE.sub(lambda m: m.group("label"), text)
    # Bazı zengin metin editörleri alt çizgiyi `\_` ve yıldızı `\*` olarak kaçırır;
    # yalnız cURL bayrak/URL/çerez bağlamında açık Markdown kaçışı varsa düzeltilir.
    if r"\_" in text and ("arena\\_acq" in text or "cf\\_clearance" in text or "dpl\\_" in text):
        text = text.replace(r"\_", "_")
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

        # Bash ANSI-C tırnaklama: $'...'
        if not cmd_mode and text[idx : idx + 2] == "$'":
            idx += 2
            buf: list[str] = []
            while idx < length and text[idx] != "'":
                if text[idx] == "\\" and idx + 1 < length:
                    esc = text[idx + 1]
                    mapping = {"n": "\n", "r": "\r", "t": "\t", "'": "'", "\\": "\\"}
                    if esc in mapping:
                        buf.append(mapping[esc])
                        idx += 2
                        continue
                    if esc == "u" and idx + 6 <= length:
                        try:
                            buf.append(chr(int(text[idx + 2 : idx + 6], 16)))
                            idx += 6
                            continue
                        except ValueError:
                            pass
                    if esc == "x" and idx + 4 <= length:
                        try:
                            buf.append(chr(int(text[idx + 2 : idx + 4], 16)))
                            idx += 4
                            continue
                        except ValueError:
                            pass
                buf.append(text[idx])
                idx += 1
            if idx < length and text[idx] == "'":
                idx += 1
            tokens.append("".join(buf))
            continue

        if text[idx] == "'" and not cmd_mode:
            idx += 1
            start = idx
            while idx < length and text[idx] != "'":
                idx += 1
            part = text[start:idx]
            if idx < length and text[idx] == "'":
                idx += 1
            # Bash `'...\''...'` bitişik kaçış kalıbını destekle
            while idx + 2 < length and text[idx : idx + 3] in (r"\''", r"\'"):
                if text[idx : idx + 3] == r"\''":
                    part += "'"
                    idx += 3
                    sub_start = idx
                    while idx < length and text[idx] != "'":
                        idx += 1
                    part += text[sub_start:idx]
                    if idx < length and text[idx] == "'":
                        idx += 1
                else:
                    break
            tokens.append(part)
            continue

        if text[idx] == '"':
            idx += 1
            buf = []
            while idx < length:
                ch = text[idx]
                if ch == "\\" and idx + 1 < length:
                    nxt = text[idx + 1]
                    if cmd_mode:
                        if nxt in ('"', "\\"):
                            buf.append(nxt)
                            idx += 2
                            continue
                    else:
                        if nxt in ('"', "\\", "$", "`"):
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

    return tokens


def parse_raw_agent_curl(raw: str) -> dict[str, Any]:
    """Ham cURL metninden URL, HTTP metodu, başlıklar ve gövdeyi ayıklar."""
    tokens = _tokenize_curl(raw)
    url = ""
    method = ""
    headers: dict[str, str] = {}
    cookie = ""
    body = ""

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "curl":
            idx += 1
            continue
        if tok in ("-H", "--header") and idx + 1 < len(tokens):
            name, sep, val = tokens[idx + 1].partition(":")
            if sep:
                headers[name.strip().lower()] = val.strip()
            idx += 2
            continue
        if tok in ("-b", "--cookie") and idx + 1 < len(tokens):
            cookie = tokens[idx + 1].strip()
            idx += 2
            continue
        if tok in ("--data-raw", "--data", "-d", "--data-binary") and idx + 1 < len(tokens):
            body = tokens[idx + 1]
            idx += 2
            continue
        if tok == "--url" and idx + 1 < len(tokens):
            url = tokens[idx + 1].strip().rstrip("^")
            idx += 2
            continue
        if tok in ("-X", "--request") and idx + 1 < len(tokens):
            method = tokens[idx + 1].strip().upper()
            idx += 2
            continue
        if tok.startswith(("http://", "https://")) and not url:
            url = tok.strip().rstrip("^")
            idx += 1
            continue
        idx += 1

    if cookie:
        headers["cookie"] = cookie
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


def extract_all_max_identities(settings: Settings) -> frozenset[str]:
    """`Settings` içindeki tüm `max` hesap yuvalarının (`1..MAX_ACCOUNT_SLOTS`) JWT `sub` kümesi."""
    identities: set[str] = set()
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
            identities.add(sub)
    return frozenset(identities)


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
    r"(?:^|\s)(?:-H|-b|-d|-X|--header|--cookie|--data-raw|--data|--data-binary|--url)$"
)


def _smart_join_two_curl_chunks(prev: str, curr: str) -> str:
    """Telegram'ın 4096 karakter sınırında böldüğü iki cURL parçasını kayıpsız birleştirir."""
    p_no_nl = prev.rstrip("\r\n")
    c_no_nl = curr.lstrip("\r\n")
    p_trim = p_no_nl.rstrip(" \t")
    c_trim = c_no_nl.lstrip(" \t")

    if not p_no_nl:
        return c_no_nl
    if not c_no_nl:
        return p_no_nl

    # 1) Satır sonu devamından (`\` veya ` ^`) hemen sonra yeni bir cURL bayrağı başlıyorsa
    if (p_trim.endswith("\\") or re.search(r"[ \t]\^$", p_no_nl)) and c_trim.startswith("-"):
        return f"{p_no_nl}\n{c_no_nl}"
    # 2) Önceki argüman kapanmış (`'` / `"` / `--compressed`) ve yeni bayrak başlıyorsa
    if c_trim.startswith("-") and (
        p_trim.endswith(("'", '"')) or p_trim.endswith("--compressed")
    ):
        return f"{p_no_nl}\n{c_no_nl}"
    # 3) Bayrak adı ile değeri tam boşluktan bölündüyse (`-H` | `"cookie:..."`)
    if _TRAILING_CURL_FLAG_RE.search(p_trim):
        return f"{p_trim} {c_trim}"
    # 4) Uzun çerez, JWT veya JSON gövdesinin tam ortasından (4096. karakterden) bölündüyse
    return f"{p_no_nl}{c_no_nl}"


def reassemble_agent_curl_chunks(
    chunks: list[str],
    *,
    chat_id_override: str | None = None,
) -> str:
    """Telegram gibi mesaj boyutu sınırlı kanallardan gelen çok parçalı cURL'ü birleştirir.

    Önce sınır-duyarlı akıllı birleştirmeyi üretir, ardından doğrudan/satırlı
    alternatiflerle birlikte `parse_agent_curl` üzerinden puanlayarak JSON gövdesi,
    çerez ve `recaptchaV3Token` bütünlüğünü en iyi koruyan adayı döndürür.
    """
    non_empty = [chunk for chunk in chunks if chunk and chunk.strip("\r\n")]
    if not non_empty:
        return ""
    if len(non_empty) == 1:
        return non_empty[0]

    smart = non_empty[0]
    for nxt in non_empty[1:]:
        smart = _smart_join_two_curl_chunks(smart, nxt)

    direct = "".join(c.strip("\r\n") for c in non_empty)
    with_newlines = "\n".join(c.strip("\r\n") for c in non_empty)
    with_spaces = " ".join(c.strip() for c in non_empty)

    candidates: list[str] = []
    for cand in (smart, direct, with_newlines, with_spaces):
        if cand not in candidates:
            candidates.append(cand)

    best_cand = smart
    best_score: tuple[int, int, int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        try:
            cfg = parse_agent_curl(cand, chat_id_override=chat_id_override)
        except ValueError:
            score = (0, 0, 0, 0, -idx)
        else:
            score = (
                1,
                -len(cfg.warnings),
                int(bool(cfg.recaptcha_token)),
                int(bool(cfg.last_user_message_id)),
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

    extra_headers: dict[str, str] = {}
    for name in PASSTHROUGH_HEADERS:
        val = headers.get(name, "").strip()
        if val:
            extra_headers[name] = val

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
