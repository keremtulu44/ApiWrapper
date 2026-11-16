"""Oturum olay defteri — "neyin ne zaman olduğu" sorusunun ölçüm katmanı.

Bu modül bir onarım aracı değildir; **kanıt** toplar. Canlı loglarda şu
soruların cevabı yoktu ve her 401'de aynı tartışma baştan yapılıyordu:

* Erişim token'ı gerçekten rotasyona giriyor mu, kaç saniyede bir?
* Bir istek hangi kimlikle gitti: sabitlenmiş `UPSTREAM_ACCESS_TOKEN` mı,
  çerezden ayıklanan mı? (`auth.resolve_bearer_token` önceliği bugüne dek kod
  okuyarak biliniyordu, veriyle değil.)
* Süresi dolmuş **görünen** bir token'la başarılı istek oldu mu? Hedefin token
  yaşını denetleyip denetlemediğinin tek gerçek kanıtı budur; JWT'nin `exp`
  iddiası tek başına kanıt değildir.
* 401 `user not found` geldiği anda çerezde taze token var mıydı?

Olaylar bellekte sınırlı bir halkada tutulur ve süreç yeniden başlayınca
sıfırlanır — bu bilinçli bir seçim: ölçüm, *yaşayan sürecin* davranışını
anlatır, diskte biriken tarihçe değil. Kalıcı toplamlar (`_stats`) halkadan
bağımsız yürür; halka dolsa bile "en yaşlı çalışan token" kaybolmaz.

Gizlilik: hiçbir olay token'ın kendisini içermez. Yalnızca kısa parmak izi
(`#a1b2c3d4e5`) ve JWT'nin `iat`/`exp` damgaları kaydedilir. Ham token
yalnızca modül içinde, kalan süreyi taze hesaplamak için tutulur; loglanmaz
ve durum dosyasına yazılmaz.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

#: Halka tamponunun varsayılan olay sayısı.
_DEFAULT_MAXLEN = 400
#: Parmak izi geçmişinin üst sınırı (bellek sızıntısını önlemek için).
_MAX_ID_HISTORY = 64
#: Rotasyon aralığı örneklerinin üst sınırı.
_MAX_INTERVAL_SAMPLES = 64

#: Sık tetiklenen olaylar için asgari kayıt aralıkları (saniye).
_UNCHANGED_MIN_INTERVAL = 300.0
_IDENTITY_MIN_INTERVAL = 900.0
_SUCCESS_MIN_INTERVAL = 60.0

#: Telegram mesaj sınırı 4096 karakter; rapor bilinçli olarak altında kalır.
_REPORT_CHAR_LIMIT = 3800


@dataclass
class _Stats:
    """Halkadan bağımsız, süreç ömrü boyunca biriken toplamlar."""

    started_at: float | None = None
    first_event_at: float | None = None
    last_event_at: float | None = None

    rotations: int = 0
    last_rotation_at: float | None = None
    rotation_intervals: list[float] = field(default_factory=list)

    current_token: dict[str, Any] = field(default_factory=dict)
    token_ids: list[str] = field(default_factory=list)
    last_identity: dict[str, Any] = field(default_factory=dict)

    successes: int = 0
    last_success_at: float | None = None
    #: Süresi dolmuş görünen bir token'la gelen en uzun *başarılı* çalışma.
    max_working_age_s: float | None = None
    max_working_age_at: float | None = None
    max_working_age_token: str | None = None
    #: `exp` aşıldıktan sonra bile çalışan isteğin aşım miktarı (sn).
    max_exp_overrun_s: float | None = None

    deaths: int = 0
    last_death_at: float | None = None
    death_body_classes: dict[str, int] = field(default_factory=dict)
    #: Ölüm anında son rotasyondan bu yana geçen süre örnekleri.
    death_rotation_gaps: list[float] = field(default_factory=list)

    forensic_outcomes: dict[str, int] = field(default_factory=dict)
    last_forensic_at: float | None = None
    last_forensic_outcome: str | None = None

    state_saves: dict[str, int] = field(default_factory=dict)


_events: deque[dict[str, Any]] = deque(maxlen=_DEFAULT_MAXLEN)
_throttle: dict[str, float] = {}
_stats = _Stats()
_maxlen = _DEFAULT_MAXLEN
_utc_offset_hours = 0.0
#: Etkin token'ın ham değeri; yalnızca `exp` hesabını tazelemek için tutulur.
_current_token: str | None = None


# --------------------------------------------------------------------------- altyapı


def configure(*, maxlen: int = _DEFAULT_MAXLEN, utc_offset_hours: float = 0.0) -> None:
    """Tampon boyutunu ve rapor saat dilimini ayarlar (açılışta bir kez)."""
    global _events, _maxlen, _utc_offset_hours
    size = max(10, int(maxlen))
    _maxlen = size
    _utc_offset_hours = float(utc_offset_hours)
    if _events.maxlen != size:
        _events = deque(list(_events)[-size:], maxlen=size)


def reset() -> None:
    """Tüm ölçümü sıfırlar (testler için)."""
    global _events, _stats, _throttle, _current_token
    _events = deque(maxlen=_maxlen)
    _stats = _Stats()
    _throttle = {}
    _current_token = None


def token_id(token: str | None) -> str | None:
    """Token'ın kısa parmak izi. Ham değer hiçbir olaya yazılmaz."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def describe_token(token: str | None, *, now: float | None = None) -> dict[str, Any]:
    """Token'ı ölçülebilir alanlara açar: parmak izi + `iat`/`exp` türevleri.

    `ttl_s` token'ın **tasarlanmış** ömrüdür (`exp - iat`), `age_s` ne kadar
    süredir elimizde olduğu, `exp_in_s` süresinin dolmasına kalan süre
    (negatifse dolmuş). Üçü birlikte "hedef yaşı denetliyor mu" sorusunu
    cevaplar: `age_s > ttl_s` olduğu hâlde başarılı istek varsa denetlemiyor.
    """
    moment = time.time() if now is None else now
    if not token:
        return {"found": False}

    from app.upstream.auth import decode_jwt_claims, looks_like_jwt

    claims = decode_jwt_claims(token) if looks_like_jwt(token) else {}
    out: dict[str, Any] = {
        "found": True,
        "id": token_id(token),
        "is_jwt": bool(claims),
        "length": len(token),
    }
    issued = claims.get("iat")
    expires = claims.get("exp")
    if isinstance(issued, (int, float)) and issued > 0:
        out["iat_utc"] = _utc_iso(float(issued))
        out["age_s"] = round(moment - float(issued), 1)
    if isinstance(expires, (int, float)) and expires > 0:
        out["exp_utc"] = _utc_iso(float(expires))
        out["exp_in_s"] = round(float(expires) - moment, 1)
        out["expired"] = float(expires) <= moment
        if isinstance(issued, (int, float)) and issued > 0:
            out["ttl_s"] = round(float(expires) - float(issued), 1)
    return out


def record(kind: str, **fields: Any) -> None:
    """Halkaya bir olay yazar (`None` alanlar atılır)."""
    now = time.time()
    event: dict[str, Any] = {"ts": round(now, 3), "utc": _utc_iso(now), "kind": kind}
    event.update({key: value for key, value in fields.items() if value is not None})
    _events.append(event)
    if _stats.first_event_at is None:
        _stats.first_event_at = now
    _stats.last_event_at = now


def _throttled(kind: str, min_interval: float, now: float) -> bool:
    """Aynı türden sık olayları seyreltir; True = kaydet."""
    previous = _throttle.get(kind)
    if previous is not None and (now - previous) < min_interval:
        return False
    _throttle[kind] = now
    return True


def _observe(token: str | None, *, now: float | None = None) -> dict[str, Any]:
    """Token'ı hem ham hem betimlenmiş olarak güncel ölçüme işler."""
    global _current_token
    described = describe_token(token, now=now)
    if described.get("found"):
        _current_token = token
        _stats.current_token = described
    return described


def snapshot(limit: int = 20) -> list[dict[str, Any]]:
    """Son olaylar, en yeni sonda."""
    items = list(_events)
    return items[-limit:] if limit and limit > 0 else items


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_time(ts: float) -> str:
    if not _utc_offset_hours:
        return ""
    shifted = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=_utc_offset_hours)
    return shifted.strftime("%H:%M:%S")


def _stamp(ts: float) -> str:
    """`12:48:33 UTC (15:48:33 yerel)` — ofset tanımsızsa yalnızca UTC."""
    local = _local_time(ts)
    text = _utc_iso(ts)[11:] + " UTC"
    return f"{text} ({local} yerel)" if local else text


# ------------------------------------------------------------------- olay kanca noktaları


def note_process_started(*, version: str | None = None) -> None:
    """Süreç açılışı: zaman çizelgesindeki 'restart' sınırını işaretler."""
    _stats.started_at = time.time()
    record("process_started", version=version)


def note_token_rotation(
    *,
    new_token: str | None,
    old_token: str | None = None,
    source: str = "set_cookie",
    slot: int = 1,
    pinned_token: str | None = None,
    replaced_pinned: bool = False,
) -> None:
    """Upstream çerezinde **farklı** bir erişim token'ı görüldü.

    `replaced_pinned=False` ve farklı bir `pinned_token` varsa sabitlenmiş
    değer taze çerezi gölgeliyor demektir: 401'in bizden kaynaklandığının
    kanıtı budur, hedefin davranışı değil.
    """
    now = time.time()
    described = _observe(new_token, now=now)
    new_id = described.get("id")
    if new_id:
        if _stats.last_rotation_at is not None:
            _stats.rotation_intervals.append(round(now - _stats.last_rotation_at, 1))
            del _stats.rotation_intervals[:-_MAX_INTERVAL_SAMPLES]
        _stats.rotations += 1
        _stats.last_rotation_at = now
        if new_id not in _stats.token_ids:
            _stats.token_ids.append(new_id)
            del _stats.token_ids[:-_MAX_ID_HISTORY]
    record(
        "token_rotated",
        source=source,
        slot=slot,
        new_id=new_id,
        old_id=token_id(old_token),
        pinned_id=token_id(pinned_token),
        replaced_pinned=replaced_pinned or None,
        exp_utc=described.get("exp_utc"),
        ttl_s=described.get("ttl_s"),
        exp_in_s=described.get("exp_in_s"),
    )
    pinned_id = token_id(pinned_token)
    if pinned_id and new_id and pinned_id != new_id and not replaced_pinned:
        pinned = describe_token(pinned_token, now=now)
        record(
            "pinned_token_shadowing",
            pinned_id=pinned_id,
            pinned_exp_utc=pinned.get("exp_utc"),
            pinned_expired=pinned.get("expired") or None,
            pinned_age_s=pinned.get("age_s"),
            fresh_id=new_id,
            hint=(
                "Çerezden taze token alındı ama UPSTREAM_ACCESS_TOKEN farklı olduğu "
                "için istekler hâlâ eski değerle gidiyor (resolve_bearer_token "
                "önceliği). Çözüm: Render env'den anahtarı SİLMEK (boşaltmak değil)."
            ),
        )


def note_cookie_unchanged(*, token: str | None, source: str = "set_cookie") -> None:
    """Çerez döndü ama içinden **aynı** token çıktı (seyreltilerek kaydedilir)."""
    now = time.time()
    described = _observe(token, now=now)
    if not _throttled("token_unchanged", _UNCHANGED_MIN_INTERVAL, now):
        return
    record(
        "token_unchanged",
        source=source,
        id=described.get("id"),
        exp_in_s=described.get("exp_in_s"),
        age_s=described.get("age_s"),
        expired=described.get("expired") or None,
    )


def note_request_identity(
    *,
    token: str | None,
    cookie_token: str | None = None,
    pinned_token: str | None = None,
    account: str | None = None,
) -> None:
    """İsteğin hangi kimlikle gideceğini kaydeder.

    Yalnızca kimlik **değiştiğinde** ya da 15 dakikada bir yazar: amaç log
    gürültüsü değil, "12:48'de hangi token'ı gönderiyorduk" sorusunun cevabı.
    """
    now = time.time()
    if not token:
        source = "none"
    elif pinned_token and pinned_token == token:
        source = "pinned"
    elif cookie_token and cookie_token == token:
        source = "cookie"
    else:
        source = "unknown"
    described = _observe(token, now=now)
    fingerprint = (source, described.get("id"), account or "")
    changed = _stats.last_identity.get("fingerprint") != fingerprint
    _stats.last_identity = {
        "fingerprint": fingerprint,
        "at": now,
        "source": source,
        "account": account,
    }
    _stats.last_identity.update(
        {key: value for key, value in described.items() if key != "found"}
    )
    # Damga her zaman basılır: ilk kayıt "değişti" diye yazıldığında sonraki
    # aynı kimlikler de 15 dakikalık pencereyi sıfırdan başlatmalı.
    interval_passed = _throttled("identity_used", _IDENTITY_MIN_INTERVAL, now)
    if not changed and not interval_passed:
        return
    record(
        "identity_used",
        source=source,
        account=account,
        id=described.get("id"),
        exp_utc=described.get("exp_utc"),
        exp_in_s=described.get("exp_in_s"),
        age_s=described.get("age_s"),
        ttl_s=described.get("ttl_s"),
        expired=described.get("expired") or None,
        changed=changed or None,
    )


def note_upstream_success(*, status: int, token: str | None, account: str | None = None) -> None:
    """Başarılı upstream yanıtı: hedefin token yaşını denetlemediğinin kanıtı buradan çıkar."""
    now = time.time()
    described = _observe(token, now=now)
    _stats.successes += 1
    _stats.last_success_at = now
    age = described.get("age_s")
    if isinstance(age, (int, float)) and (
        _stats.max_working_age_s is None or age > _stats.max_working_age_s
    ):
        _stats.max_working_age_s = float(age)
        _stats.max_working_age_at = now
        _stats.max_working_age_token = described.get("id")
    remaining = described.get("exp_in_s")
    if isinstance(remaining, (int, float)) and remaining < 0:
        overrun = abs(float(remaining))
        if _stats.max_exp_overrun_s is None or overrun > _stats.max_exp_overrun_s:
            _stats.max_exp_overrun_s = overrun
    if not _throttled("upstream_ok", _SUCCESS_MIN_INTERVAL, now):
        return
    record(
        "upstream_ok",
        status=status,
        account=account,
        id=described.get("id"),
        age_s=age,
        exp_in_s=remaining,
        ttl_s=described.get("ttl_s"),
    )


def note_identity_rejected(
    *,
    status: int,
    body_class: str,
    token: str | None,
    account: str | None = None,
) -> None:
    """Upstream kimliği reddetti. `body_class='user_not_found'` = oturum ölümü."""
    now = time.time()
    described = _observe(token, now=now)
    gap = (
        round(now - _stats.last_rotation_at, 1) if _stats.last_rotation_at is not None else None
    )
    if body_class == "user_not_found":
        _stats.deaths += 1
        _stats.last_death_at = now
        if gap is not None:
            _stats.death_rotation_gaps.append(gap)
            del _stats.death_rotation_gaps[:-_MAX_INTERVAL_SAMPLES]
    _stats.death_body_classes[body_class] = _stats.death_body_classes.get(body_class, 0) + 1
    record(
        "identity_rejected",
        status=status,
        body_class=body_class,
        account=account,
        id=described.get("id"),
        age_s=described.get("age_s"),
        exp_in_s=described.get("exp_in_s"),
        ttl_s=described.get("ttl_s"),
        seconds_since_last_rotation=gap,
    )


def note_forensic_retry(*, outcome: str, **fields: Any) -> None:
    """Ölüm anındaki tek doğrulama denemesinin sonucu.

    `recovered` → sorun sabitlenmiş bayat token'dı (bizim taraf).
    `rejected` → oturum gerçekten ölü (yeni cURL şart).
    `skipped_no_fresh_token` → çerez döndü ama içinde taze token yok: hedef
    rotasyonu durdurmuş, yani ölüm sunucu tarafında.
    """
    now = time.time()
    _stats.forensic_outcomes[outcome] = _stats.forensic_outcomes.get(outcome, 0) + 1
    _stats.last_forensic_at = now
    _stats.last_forensic_outcome = outcome
    record("forensic_retry", outcome=outcome, **fields)


def note_state_saved(*, source: str, token: str | None = None) -> None:
    """Durum deposuna yazım: cURL/çerez yenileme sınırlarını çizelgeye işler."""
    _stats.state_saves[source] = _stats.state_saves.get(source, 0) + 1
    described = describe_token(token)
    record("state_saved", source=source, id=described.get("id"), exp_in_s=described.get("exp_in_s"))


# --------------------------------------------------------------------------- çözümleme


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _identity_summary() -> dict[str, Any]:
    """Son kullanılan kimlik; iç parmak izi atılır, damga okunur biçime çevrilir."""
    out = {key: value for key, value in _stats.last_identity.items() if key != "fingerprint"}
    at = out.pop("at", None)
    if isinstance(at, (int, float)):
        out["at_utc"] = _utc_iso(float(at))
    return out


def analysis(*, now: float | None = None) -> dict[str, Any]:
    """Toplanmış ölçüm + otomatik yorum. `/v1/admin/timeline` bunu döner."""
    moment = time.time() if now is None else now
    current = (
        describe_token(_current_token, now=moment)
        if _current_token
        else dict(_stats.current_token)
    )
    since_rotation = (
        round(moment - _stats.last_rotation_at, 1)
        if _stats.last_rotation_at is not None
        else None
    )
    data: dict[str, Any] = {
        "buffer": {"events": len(_events), "capacity": _maxlen},
        "window": {
            "process_started": _utc_iso(_stats.started_at) if _stats.started_at else None,
            "first_event": _utc_iso(_stats.first_event_at) if _stats.first_event_at else None,
            "last_event": _utc_iso(_stats.last_event_at) if _stats.last_event_at else None,
            "hours": (
                round((moment - _stats.first_event_at) / 3600, 2) if _stats.first_event_at else None
            ),
        },
        "token": {
            "current": current,
            "distinct_ids": len(_stats.token_ids),
            "rotations": _stats.rotations,
            "last_rotation": _utc_iso(_stats.last_rotation_at) if _stats.last_rotation_at else None,
            "seconds_since_last_rotation": since_rotation,
            "rotation_interval_mean_s": _mean(_stats.rotation_intervals),
        },
        "identity_last_used": _identity_summary(),
        "upstream": {
            "successes": _stats.successes,
            "last_success": _utc_iso(_stats.last_success_at) if _stats.last_success_at else None,
            "max_working_age_s": _stats.max_working_age_s,
            "max_working_age_at": (
                _utc_iso(_stats.max_working_age_at) if _stats.max_working_age_at else None
            ),
            "max_working_age_token": _stats.max_working_age_token,
            "max_exp_overrun_s": _stats.max_exp_overrun_s,
            "deaths": _stats.deaths,
            "last_death": _utc_iso(_stats.last_death_at) if _stats.last_death_at else None,
            "body_classes": dict(_stats.death_body_classes),
            "death_rotation_gap_mean_s": _mean(_stats.death_rotation_gaps),
        },
        "forensic": {
            "outcomes": dict(_stats.forensic_outcomes),
            "last": _utc_iso(_stats.last_forensic_at) if _stats.last_forensic_at else None,
            "last_outcome": _stats.last_forensic_outcome,
        },
        "state_saves": dict(_stats.state_saves),
    }
    data["verdict"] = verdict_text(data)
    return data


def verdict_text(data: dict[str, Any]) -> str:
    """Üç hipotezi ayırt eden otomatik yorum.

    H1: hedef artık `exp`'i denetliyor.
    H2: oturum sunucu tarafında düşürüldü (rotasyon durur, taze token gelmez).
    H3: sabitlenmiş `UPSTREAM_ACCESS_TOKEN` taze çerezi gölgeliyor.
    """
    forensic = data.get("forensic") or {}
    outcomes: dict[str, int] = forensic.get("outcomes") or {}
    upstream = data.get("upstream") or {}
    token = data.get("token") or {}
    deaths = int(upstream.get("deaths") or 0)
    successes = int(upstream.get("successes") or 0)
    overrun = upstream.get("max_exp_overrun_s")
    max_age = upstream.get("max_working_age_s")
    ttl = (token.get("current") or {}).get("ttl_s")

    if outcomes.get("recovered"):
        return (
            "H3 DOĞRULANDI: 401'den sonra çerezdeki taze token'la istek başarılı oldu; "
            "sorun sabitlenmiş UPSTREAM_ACCESS_TOKEN'ın bayat kalması. Çözüm: Render "
            "env'den bu anahtarı SİL (boşaltma, sil)."
        )
    if outcomes.get("rejected"):
        return (
            "H2 DOĞRULANDI: taze çerez token'ı da 401 verdi; oturum sunucu tarafında ölü. "
            "Çözüm: tarayıcıdan yeni cURL al, /curl ile uygula. /yenile diriltmez."
        )
    if outcomes.get("skipped_no_fresh_token"):
        return (
            "H2 DESTEKLENDİ: 401 anında çerez döndü ama içinde taze token yoktu; rotasyon "
            "durmuş. Ölüm bizim ayarımızdan değil, oturumun düşmesinden."
        )
    if isinstance(overrun, (int, float)) and overrun > 60:
        base = (
            f"H1 ÇÜRÜTÜLDÜ: token `exp` süresini {int(overrun)} sn aşmışken istek başarılı "
            "oldu — hedef yaşı denetlemiyor."
        )
        if deaths:
            return (
                f"{base} Ama {deaths} ölüm kayıtlı: sebep süre değil, oturumun düşmesi. "
                "Taze cURL gerekir."
            )
        return base
    if (
        deaths
        and isinstance(max_age, (int, float))
        and isinstance(ttl, (int, float))
        and ttl > 0
    ):
        if max_age > ttl * 2:
            return (
                f"H1 ÇÜRÜTÜLDÜ: token {int(max_age)} sn yaşındayken çalıştı (tasarlanmış ömür "
                f"{int(ttl)} sn). Ölümler süre kaynaklı değil."
            )
        return (
            "H1 OLASI: kayıtlı en yaşlı başarılı token tasarlanmış ömrü aşmadı ve "
            f"{deaths} ölüm var. Kesin karar için forensic retry açık kalsın "
            "(UPSTREAM_FORENSIC_RETRY=true) ve bir ölüm yakalansın."
        )
    if successes and not deaths:
        return (
            f"Sağlıklı: {successes} başarılı istek, ölüm yok. Token "
            f"{int(token.get('rotations') or 0)} kez rotasyona girdi."
        )
    if not successes and not deaths:
        return "Yetersiz veri: bu süreçte henüz upstream isteği ölçülmedi."
    return (
        f"{deaths} ölüm, {successes} başarılı istek; forensic retry hiç tetiklenmedi. "
        "Bir sonraki ölümü beklemek yerine /tani ile etkin kimliği doğrula."
    )


def format_report(*, limit: int = 10, now: float | None = None) -> str:
    """Telegram `/zaman` için insan okunur Türkçe rapor."""
    moment = time.time() if now is None else now
    data = analysis(now=moment)
    token: dict[str, Any] = data["token"]
    upstream: dict[str, Any] = data["upstream"]
    forensic: dict[str, Any] = data["forensic"]
    window: dict[str, Any] = data["window"]
    current: dict[str, Any] = token.get("current") or {}
    identity: dict[str, Any] = data.get("identity_last_used") or {}

    lines: list[str] = ["🧾 OTURUM OLAY DEFTERİ"]
    hours = window.get("hours")
    lines.append(
        f"Pencere: {window.get('first_event') or '—'} → {window.get('last_event') or '—'} UTC"
        + (f" ({hours} sa)" if hours else "")
    )
    if window.get("process_started"):
        lines.append(f"Süreç açılışı: {window['process_started']} UTC")

    lines.append(
        f"Token: {token.get('rotations')} rotasyon · {token.get('distinct_ids')} farklı kimlik"
        f" · son {token.get('last_rotation') or '—'} UTC"
    )
    since = token.get("seconds_since_last_rotation")
    if since is not None:
        lines.append(f"Son rotasyondan bu yana: {int(since)} sn")
    if token.get("rotation_interval_mean_s"):
        lines.append(f"Ortalama rotasyon aralığı: {int(token['rotation_interval_mean_s'])} sn")
    if current.get("found"):
        bits = [f"#{current.get('id')}"]
        if current.get("exp_utc"):
            bits.append(f"exp {current['exp_utc']} UTC")
        if current.get("exp_in_s") is not None:
            bits.append(
                f"{int(abs(current['exp_in_s']))} sn "
                + ("geçmiş" if current.get("expired") else "kaldı")
            )
        if current.get("ttl_s"):
            bits.append(f"ömür {int(current['ttl_s'])} sn")
        if current.get("age_s"):
            bits.append(f"yaş {int(current['age_s'])} sn")
        lines.append("Şu an: " + " · ".join(bits))
    if identity:
        lines.append(
            f"Son kullanılan kimlik: {identity.get('source') or '—'}"
            + (f" · hesap {identity.get('account')}" if identity.get("account") else "")
        )

    lines.append(
        f"Upstream: {upstream.get('successes')} başarılı · {upstream.get('deaths')} ölüm"
        f" · son başarı {upstream.get('last_success') or '—'} UTC"
    )
    if upstream.get("max_working_age_s"):
        lines.append(
            f"En yaşlı ÇALIŞAN token: {int(upstream['max_working_age_s'])} sn"
            f" ({round(upstream['max_working_age_s'] / 3600, 1)} sa)"
            f" #{upstream.get('max_working_age_token')}"
        )
    if upstream.get("max_exp_overrun_s"):
        lines.append(
            f"`exp` aşımıyla çalışan istek: {int(upstream['max_exp_overrun_s'])} sn "
            "sonra bile başarılı ⇒ hedef yaşı denetlemiyor"
        )
    if upstream.get("body_classes"):
        lines.append(f"Ret sınıfları: {upstream['body_classes']}")
    if forensic.get("outcomes"):
        lines.append(f"Forensic retry: {forensic['outcomes']} · son {forensic.get('last_outcome')}")

    events = snapshot(limit)
    if events:
        lines.append(f"Son {len(events)} olay:")
        for event in reversed(events):
            detail = ", ".join(
                f"{key}={value}"
                for key, value in event.items()
                if key not in {"ts", "utc", "kind"} and not isinstance(value, dict)
            )
            line = f" {_stamp(float(event['ts']))} {event['kind']}"
            lines.append((line + (f" {detail}" if detail else ""))[:220])

    lines.append(f"YORUM: {data.get('verdict')}")
    text = "\n".join(lines)
    return text[: _REPORT_CHAR_LIMIT - 1] + "…" if len(text) > _REPORT_CHAR_LIMIT else text
