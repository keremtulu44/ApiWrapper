"""Upstream kota/kısıtlama tespiti.

Hedef servis hesabı kilitlediğinde bunu farklı biçimlerde dile getirebiliyor:

1. HTTP hata gövdesinde (örn. ``429 {"error":"upstream limit reached"}``),
2. Akış içinde hata olayı olarak (``3:"upstream limit reached"``),
3. **Düz metin olarak** (``0:"upstream limit reached"``) — bu durumda metin normal
   bir asistan cevabı gibi görünür ve başka hiçbir katman onu hata saymaz.

Bu modül üçüncü yol da dâhil üçünü de yakalamak için gereken ortak parçaları sağlar.
İşaretler koda gömülü değildir; ``UPSTREAM_LIMIT_MARKERS`` ile ayarlanır.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


def normalize_markers(markers: Iterable[str] | None) -> tuple[str, ...]:
    """İşaret listesini normalize eder: boş olanlar atılır, küçük harfe çevrilir."""
    if not markers:
        return ()
    return tuple(m.strip().lower() for m in markers if m and m.strip())


def find_quota_marker(text: str, markers: Iterable[str]) -> str | None:
    """Metinde kota işareti arar; bulursa eşleşen işareti döndürür.

    Karşılaştırma büyük/küçük harf duyarsızdır; işaretler zaten küçük harfe
    normalize edilmiştir ama çağıranın ham liste vermesi durumunda da çalışır.
    """
    if not text:
        return None
    lowered = text.lower()
    for marker in markers:
        if marker and marker in lowered:
            return marker
    return None


def quota_error_message(marker: str, source: str, detail: str = "") -> str:
    """İstemciye dönen, ne yapılacağını söyleyen hata metni üretir."""
    text = (
        f"The upstream service has temporarily limited this account "
        f"(matched {marker!r} in the {source}). The rolling window needs to expire "
        "before requests succeed again; the wrapper will keep honouring Retry-After."
    )
    if detail:
        text = f"{text} Upstream said: {detail[:300]}"
    return text


class QuotaTextScanner:
    """Yanıtın **ilk karakterlerinde** kota metni arar.

    Bazı uçlar kısıtlama mesajını hata olayı yerine düz metin delta'sı olarak
    gönderir. Metin istemciye bir kez iletildikten sonra geri alınamayacağı için
    tarama yalnızca (a) henüz hiçbir şey yayınlanmamışken ve (b) yapılandırılabilir
    bir pencere içinde yapılır. Pencere dolduğunda ya da gerçek içerik akmaya
    başladığında tarama kalıcı olarak durur — böylece modelin meşru çıktısında
    geçen "rate limit" gibi ifadeler yanlış alarma yol açmaz.
    """

    __slots__ = ("_buffer", "_disabled", "_markers", "_window")

    def __init__(self, markers: Iterable[str], window: int = 300) -> None:
        self._markers = normalize_markers(markers)
        self._window = max(0, int(window))
        self._buffer = ""
        self._disabled = not self._markers or self._window == 0

    @property
    def active(self) -> bool:
        """Tarama hâlâ çalışıyor mu?"""
        return not self._disabled

    def disable(self) -> None:
        """İçerik yayınlandı; artık geri dönüş yok, taramayı bırak."""
        self._disabled = True
        self._buffer = ""

    def feed(self, text: str) -> str | None:
        """Yeni metin parçasını işler; kota işareti bulunursa işareti döndürür.

        Kısmi eşleşme olabilecek kuyruk tamponda bekletilir, çünkü işaret iki
        delta arasına bölünmüş olabilir (``"limit re"`` + ``"ached"``).
        """
        if self._disabled or not text:
            return None
        self._buffer += text
        marker = find_quota_marker(self._buffer, self._markers)
        if marker is not None:
            logger.warning(
                "upstream_quota_detected_in_stream", marker=marker, preview=self._buffer[:120]
            )
            return marker
        # Pencere dolduysa daha fazla tarama; gerçek içerik akıyor demektir.
        if len(self._buffer) > self._window:
            self.disable()
            return None
        # Kısmi eşleşme olabilecek kuyruğu tut, gerisini yayınlanabilir say.
        hold = max((len(m) for m in self._markers), default=0) - 1
        if hold > 0:
            self._buffer = self._buffer[-hold:]
        return None
