"""Metin onarımı: ikiye bölünmüş UTF-16 vekil (surrogate) çiftleri.

Upstream cevabı JavaScript'te üretir; JavaScript dizeleri UTF-16'dır ve BMP dışı
bir karakter (çoğu emoji) iki kod biriminden — bir "üst" ve bir "alt" vekilden —
oluşur. Böyle bir karakter iki akış parçasına bölünürse her parça JSON'da
``"\\ud83d"`` gibi tek başına bir vekil taşır. Python bunu tek başına bir kod
noktası olarak tutar; iki yarıyı yan yana getirmek onları birleştirmez ve tek
başına vekil UTF-8'e **çevrilemez** (`UnicodeEncodeError`).

Gerçek sunucuda ölçülen sonuç: akışsız uç HTTP 500 dönüyor, akışlı uç ilk
parçadan sonra kesiliyordu. Buradaki yardımcılar yarımları birleştirir; eşi
gelmeyen yarımı U+FFFD ile değiştirir.

Vekil içermeyen metin (olağan durum) olduğu gibi, kopyalanmadan geri verilir.
"""

from __future__ import annotations

import re

_SURROGATE = re.compile("[\ud800-\udfff]")
_HIGH_FIRST, _HIGH_LAST = "\ud800", "\udbff"


def repair_surrogates(text: str) -> str:
    """Bitişik vekil çiftlerini gerçek karaktere, eşsiz vekilleri U+FFFD'ye çevirir."""
    if not text or _SURROGATE.search(text) is None:
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


class SurrogateJoiner:
    """Parça parça gelen metinde iki parçaya bölünmüş vekil çiftlerini birleştirir.

    Bir parça üst vekille bitiyorsa o karakter bekletilir ve sonraki parçanın
    başına eklenir. Akış bitince `flush()` çağrılır.
    """

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held = ""

    def feed(self, chunk: str) -> str:
        """Yayınlanabilir metni döndürür (bekletilen yarım hariç)."""
        if not self._held and (not chunk or _SURROGATE.search(chunk) is None):
            return chunk
        text = self._held + chunk
        self._held = ""
        if text and _HIGH_FIRST <= text[-1] <= _HIGH_LAST:
            text, self._held = text[:-1], text[-1]
        return repair_surrogates(text)

    def flush(self) -> str:
        """Akış sonunda bekleyen yarım varsa onun yerine U+FFFD döndürür."""
        held, self._held = self._held, ""
        return repair_surrogates(held)
