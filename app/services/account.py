"""Upstream hesap havuzu: çoklu çerez, kota penceresi ve AIMD ile öğrenilen bütçe.

`scripts/curl_to_env.py` her cURL'ü ayrı bir hesap yuvasına yazar (1. hesap
soneksiz, 2. hesap ``_2`` soneksli anahtarlar). Bu modül o yuvaları okur ve
istekleri hesaplar arasında dağıtır.

Tasarım notları:

* **Bütçe yumuşaktır.** Upstream'in gerçek sınırını bilmiyoruz; ``account_msg_budget``
  yalnızca hangi hesabın tercih edileceğini belirler, isteği engellemez. Sert kısıt
  yalnızca gerçek bir kilit algılandığında devreye giren cooldown'dur.
* **AIMD ile öğrenme.** Bir hesap pencere içinde N. mesajda kilitlendiyse gerçek
  sınır N'den küçüktür; öğrenilen tavan ``N - 1`` olur (çarpan azaltma). Pencere
  kilit olmadan dolduğunda tavan bir mesaj artırılır (toplama artırma).
* **Amaç nazik olmak.** Kilitli hesapla zorlamak kilit süresini uzatır; bu yüzden
  kilit algılanınca o hesap pencerenin sonuna kadar dinlendirilir ve istek başka
  hesapla devam eder.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.core.logging import get_logger
from app.core.metrics import metrics

logger = get_logger(__name__)

#: Hesap başına okunan `.env` alanları → `Settings` alan adları.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("TARGET_DOMAIN", "target_domain"),
    ("UPSTREAM_COOKIE", "upstream_cookie"),
    ("UPSTREAM_ACCESS_TOKEN", "upstream_access_token"),
    ("UPSTREAM_AUTH_FROM_COOKIE", "upstream_auth_from_cookie"),
    ("UPSTREAM_AUTH_SCHEME", "upstream_auth_scheme"),
    ("UPSTREAM_TOKEN_COOKIE_NAMES", "upstream_token_cookie_names"),
    ("UPSTREAM_USER_AGENT", "upstream_user_agent"),
    ("UPSTREAM_ACCEPT_LANGUAGE", "upstream_accept_language"),
    ("UPSTREAM_REFERER_PATH", "upstream_referer_path"),
)


def _suffix(slot: int) -> str:
    return "" if slot <= 1 else f"_{slot}"


def _bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


#: cURL'de bulunmadığında mevcut değeri koruyan hesap alanları.
_KEEP_IF_EMPTY = frozenset(
    {"recaptcha_token", "user_agent", "accept_language", "referer_path", "target_domain"}
)


@dataclass(slots=True)
class Account:
    """`.env`'deki bir hesap yuvası."""

    slot: int
    name: str
    cookie: str = ""
    access_token: str = ""
    auth_from_cookie: bool = False
    auth_scheme: str = "Bearer"
    token_cookie_names: list[str] = field(default_factory=list)
    recaptcha_token: str = ""
    user_agent: str = ""
    accept_language: str = ""
    referer_path: str = ""
    target_domain: str = ""

    @property
    def label(self) -> str:
        return self.name or f"hesap-{self.slot}"

    @property
    def is_configured(self) -> bool:
        """İstek gönderebilecek kadar bilgi var mı?"""
        return bool(self.cookie.strip() or self.access_token.strip())

    @property
    def has_own_captcha_token(self) -> bool:
        return bool(self.recaptcha_token.strip())

    @classmethod
    def from_env(cls, settings: Settings, slot: int) -> Account:
        """`.env`'den bir hesap yuvasını okur (1. yuva soneksizdir)."""
        suffix = _suffix(slot)

        def value(key: str) -> str:
            return str(getattr(settings, f"{key.lower()}{suffix}", "") or "").strip()

        # Hesap adı anahtarı bu kalıba uymaz: araç `UPSTREAM_ACCOUNT_2_NAME` yazar
        # (soneks sonda değil, `_NAME`'den önce).
        name_attr = "upstream_account_name" if slot <= 1 else f"upstream_account_{slot}_name"

        return cls(
            slot=slot,
            name=str(getattr(settings, name_attr, "") or "").strip() or f"hesap-{slot}",
            cookie=value("UPSTREAM_COOKIE"),
            access_token=value("UPSTREAM_ACCESS_TOKEN"),
            auth_from_cookie=_bool(value("UPSTREAM_AUTH_FROM_COOKIE")),
            auth_scheme=value("UPSTREAM_AUTH_SCHEME") or "Bearer",
            token_cookie_names=_csv(value("UPSTREAM_TOKEN_COOKIE_NAMES")),
            recaptcha_token=value("RECAPTCHA_STATIC_TOKEN"),
            user_agent=value("UPSTREAM_USER_AGENT"),
            accept_language=value("UPSTREAM_ACCEPT_LANGUAGE"),
            referer_path=value("UPSTREAM_REFERER_PATH"),
            target_domain=value("TARGET_DOMAIN"),
        )

    def adopt_credentials(self, fresh: Account) -> list[str]:
        """Kimlik alanlarını başka bir hesap tanımından **yerinde** devralır.

        `AccountPool` referansları uygulama geneline dağılmıştır; hesap nesnesini
        değiştirmek yerine alanlarını güncelliyoruz ki yeni cURL hiçbir yerde
        yeniden başlatma gerektirmesin. Değişen alan adlarını döndürür.
        """
        changed: list[str] = []
        for name in (
            "cookie",
            "access_token",
            "auth_from_cookie",
            "auth_scheme",
            "recaptcha_token",
            "user_agent",
            "accept_language",
            "referer_path",
            "target_domain",
        ):
            old = getattr(self, name)
            new = getattr(fresh, name)
            # cURL'de bulunmayan alanlar mevcut değerini korusun.
            if name in _KEEP_IF_EMPTY and not new:
                continue
            if old != new:
                setattr(self, name, new)
                changed.append(name)
        self.token_cookie_names = list(fresh.token_cookie_names)
        return changed

    def effective_settings(self, base: Settings) -> Settings:
        """Bu hesabın kimlik bilgilerini taşıyan bir `Settings` kopyası üretir.

        `model_copy` orijinali değiştirmez; `origin`/`base_url`/`stream_url` gibi
        özellikler yeni alan değerlerinden yeniden hesaplanır.
        """
        updates: dict[str, object] = {
            "upstream_cookie": self.cookie,
            "upstream_access_token": self.access_token,
            "upstream_auth_from_cookie": self.auth_from_cookie,
            "upstream_auth_scheme": self.auth_scheme,
            "upstream_token_cookie_names": list(self.token_cookie_names),
            "recaptcha_static_token": self.recaptcha_token,
        }
        if self.user_agent:
            updates["upstream_user_agent"] = self.user_agent
        if self.accept_language:
            updates["upstream_accept_language"] = self.accept_language
        if self.referer_path:
            updates["upstream_referer_path"] = self.referer_path
        if self.target_domain:
            updates["target_domain"] = self.target_domain
        return base.model_copy(update=updates)


@dataclass(slots=True)
class AccountState:
    """Bir hesabın çalışma zamanı durumu."""

    timestamps: deque[float] = field(default_factory=deque)
    cooldown_until: float = 0.0
    learned_limit: int = 0
    clean_windows: int = 0
    quota_hits: int = 0
    total_messages: int = 0
    last_used_at: float = 0.0


class AccountPool:
    """Hesapları seçer, kota penceresini sayar ve bütçeyi öğrenir."""

    def __init__(self, settings: Settings, accounts: list[Account] | None = None) -> None:
        self.settings = settings
        self.accounts: list[Account] = (
            accounts if accounts is not None else load_accounts(settings)  # noqa: F821
        )
        self._states: dict[int, AccountState] = {
            account.slot: AccountState() for account in self.accounts
        }
        self._cursor = 0

    # ------------------------------------------------------------ yükleme
    @property
    def size(self) -> int:
        return len(self.accounts)

    def get(self, slot: int) -> Account | None:
        return next((a for a in self.accounts if a.slot == slot), None)

    def state(self, slot: int) -> AccountState:
        return self._states.setdefault(slot, AccountState())

    def reload_credentials(self, settings: Settings) -> dict[str, list[str]]:
        """Ayarlardan hesap kimliklerini yeniden okur ve yerinde uygular."""
        changed: dict[str, list[str]] = {}
        for account in self.accounts:
            fresh = Account.from_env(settings, account.slot)
            fields = account.adopt_credentials(fresh)
            if fields:
                changed[account.label] = sorted(fields)
        return changed

    # ------------------------------------------------------------- pencere
    def _prune(self, state: AccountState, now: float) -> None:
        window = max(1.0, self.settings.account_quota_window_seconds)
        while state.timestamps and now - state.timestamps[0] > window:
            state.timestamps.popleft()

    def messages_in_window(self, slot: int, now: float | None = None) -> int:
        state = self.state(slot)
        self._prune(state, time.time() if now is None else now)
        return len(state.timestamps)

    def cooldown_remaining(self, slot: int, now: float | None = None) -> float:
        current = time.time() if now is None else now
        return max(0.0, self.state(slot).cooldown_until - current)

    def effective_budget(self, slot: int) -> int:
        """Öğrenilen tavan varsa onu, yoksa yapılandırılmış bütçeyi döndürür."""
        learned = self.state(slot).learned_limit
        return learned if learned > 0 else max(1, self.settings.account_msg_budget)

    # -------------------------------------------------------------- seçim
    def pick(
        self, now: float | None = None, exclude_slot: int | None = None
    ) -> Account | None:
        """Kullanılabilir en uygun hesabı döndürür; hepsi dinlenmedeyse `None`.

        Sıralama ölçütü: pencere içi mesaj sayısı (az olan önce), eşitlikte en
        uzun süredir kullanılmayan. Böylece bütçe bir engelden çok bir tercih
        sırası olarak çalışır — tek hesaplı kurulumda istek hiçbir zaman
        reddedilmez.

        ``exclude_slot`` verilirse o yuva (örn. az önce 401 alan hesap) atlanır;
        aksi halde devir aynı hesabı seçip sonsuz döngü üretebilirdi.
        """
        current = time.time() if now is None else now
        usable = [
            account
            for account in self.accounts
            if account.is_configured
            and account.slot != exclude_slot
            and self.cooldown_remaining(account.slot, current) <= 0
        ]
        if not usable:
            return None
        usable.sort(
            key=lambda a: (
                self.messages_in_window(a.slot, current),
                self.state(a.slot).last_used_at,
            )
        )
        return usable[0]

    def next_cooldown_end(self, now: float | None = None) -> float:
        """Cooldown'daki hesapların en erken açılma anına kalan süre."""
        current = time.time() if now is None else now
        remaining = [
            self.cooldown_remaining(a.slot, current)
            for a in self.accounts
            if self.cooldown_remaining(a.slot, current) > 0
        ]
        return min(remaining) if remaining else 0.0

    # ------------------------------------------------------------- kayıt
    def record_message(self, slot: int, now: float | None = None) -> None:
        """Başarılı bir upstream isteğini pencereye işler."""
        current = time.time() if now is None else now
        state = self.state(slot)
        self._prune(state, current)
        state.timestamps.append(current)
        state.last_used_at = current
        state.total_messages += 1

        # AIMD (toplama artırma): tavan ancak bütçe kadar mesaj **kilit olmadan**
        # gönderildikten sonra ve en fazla pencere başına bir kez artar. Her
        # mesajda artırmak gerçek limiti hızla aşıp kilit üretirdi.
        budget = self.effective_budget(slot)
        growth = max(1, self.settings.account_budget_growth_streak)
        if len(state.timestamps) >= budget * (1 + state.clean_windows):
            state.clean_windows += 1
            if state.clean_windows >= growth:
                state.learned_limit = budget + 1
                state.clean_windows = 0  # sıfırlanmazsa bir sonraki mesajda yine artar
                logger.info(
                    "account_budget_raised",
                    account=self._label(slot),
                    learned_limit=state.learned_limit,
                )

        metrics.set_gauge(
            "apiwrapper_account_messages_in_window",
            float(len(state.timestamps)),
            {"account": self._label(slot)},
        )

    def report_quota(self, slot: int, now: float | None = None) -> None:
        """Kilit algılandı: hesabı dinlendir ve bütçeyi düşür (AIMD: multiplicative)."""
        current = time.time() if now is None else now
        state = self.state(slot)
        self._prune(state, current)
        count = len(state.timestamps)
        state.quota_hits += 1
        state.clean_windows = 0  # AIMD: çarpan azaltma, birikmiş krediyi sıfırlar
        state.cooldown_until = current + max(1.0, self.settings.account_cooldown_seconds)
        # N. mesajda kilitlendiysek gerçek sınır en fazla N - 1'dir.
        if count > 0:
            state.learned_limit = max(1, count - 1)
        logger.warning(
            "account_cooled_down",
            account=self._label(slot),
            messages_in_window=count,
            learned_limit=state.learned_limit,
            cooldown_seconds=self.settings.account_cooldown_seconds,
        )
        metrics.inc(
            "apiwrapper_account_switches_total", labels={"reason": "quota"}
        )
        metrics.set_gauge(
            "apiwrapper_account_cooldown", 1.0, {"account": self._label(slot)}
        )

    def clear_cooldown(self, slot: int) -> None:
        self.state(slot).cooldown_until = 0.0
        metrics.set_gauge(
            "apiwrapper_account_cooldown", 0.0, {"account": self._label(slot)}
        )

    def _label(self, slot: int) -> str:
        account = self.get(slot)
        return account.label if account else f"hesap-{slot}"

    # --------------------------------------------------------- gözlemlenebilirlik
    def snapshot(self, now: float | None = None) -> list[dict[str, object]]:
        current = time.time() if now is None else now
        out: list[dict[str, object]] = []
        for account in self.accounts:
            state = self.state(account.slot)
            in_window = self.messages_in_window(account.slot, current)
            remaining = self.cooldown_remaining(account.slot, current)
            out.append(
                {
                    "slot": account.slot,
                    "name": account.label,
                    "configured": account.is_configured,
                    "target_domain": account.target_domain or self.settings.target_domain,
                    "has_cookie": bool(account.cookie.strip()),
                    "has_access_token": bool(account.access_token.strip()),
                    "has_own_captcha_token": account.has_own_captcha_token,
                    "messages_in_window": in_window,
                    "effective_budget": self.effective_budget(account.slot),
                    "learned_limit": state.learned_limit or None,
                    "quota_window_seconds": self.settings.account_quota_window_seconds,
                    "cooldown_remaining_seconds": round(remaining, 1),
                    "quota_hits": state.quota_hits,
                    "total_messages": state.total_messages,
                }
            )
        return out


def load_accounts(settings: Settings) -> list[Account]:
    """`.env`'deki dolu hesap yuvalarını okur.

    Bir yuva, isteği kimlikle gönderebilecek bilgiye (cookie veya access_token)
    sahipse "dolu" sayılır. Ara yuvalar boşsa atlanır; `--reset-accounts` sonrası
    arta kalan adlar hesap sayılmaz.
    """
    accounts: list[Account] = []
    for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
        account = Account.from_env(settings, slot)
        if account.is_configured:
            accounts.append(account)
    return accounts


def build_account_pool(settings: Settings) -> AccountPool:
    """Havuzu kurar; tutarsızlıkları loglar."""
    pool = AccountPool(settings)
    configured = [a for a in pool.accounts if a.is_configured]
    domains = {a.target_domain for a in configured if a.target_domain}
    if len(domains) > 1:
        logger.warning(
            "account_pool_domain_mismatch",
            domains=sorted(domains),
            hint=(
                "Havuz aynı servisin farklı hesaplarını bekler. Farklı domain'ler "
                "farklı upstream'lere istek gönderir."
            ),
        )
    shared_captcha = [a.label for a in configured if not a.has_own_captcha_token]
    if len(configured) > 1 and shared_captcha and settings.recaptcha_provider == "static":
        logger.warning(
            "account_pool_shared_captcha_token",
            accounts=shared_captcha,
            hint=(
                "Bu hesapların kendi RECAPTCHA_STATIC_TOKEN değeri yok; global token "
                "kullanılacak. Token tarayıcı oturumuna bağlıdır ve ~2 dk ömürlüdür — "
                "hesap başına taze cURL ile token alın."
            ),
        )
    if configured:
        logger.info(
            "account_pool_ready",
            accounts=[a.label for a in configured],
            budget=settings.account_msg_budget,
            window_seconds=settings.account_quota_window_seconds,
        )
    return pool
