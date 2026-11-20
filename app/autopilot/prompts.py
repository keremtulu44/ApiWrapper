"""Durumsuz `max` için rol kuralları, hafıza paketi derleyicisi ve karar çözücüsü.

`max` tarafında konuşma geçmişi tutulmaz ve her turda tek bir istek hakkı vardır.
Bu modül her turda `max`'a:
1. Projenin asıl amacını ve kalite ilkelerini,
2. Önceki turların sıkıştırılmış hafıza defterini ve tur izini,
3. Kodlama ajanının son yanıtını **ve** sandbox'tan çekilen gerçek araç/komut
   çalışma kanıtlarını
tek bir odaklı bağlam paketi olarak sunar; dönüşte üretilen yapısal kararı
ayrıştırır.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

#: Ajanın her zaman uyması gereken çalışma kuralları (ilk mesajın sonuna eklenir).
AGENT_OPERATING_GUARDRAILS = (
    "\n\nNot: Çalışırken bana çoktan seçmeli soru/onay penceresi (ask_user, "
    "offer_options vb.) açma; teknik detaylarda en doğru ve sağlam kararı kendin "
    "verip doğrudan uygula. Şu an dışarıda olduğum için kendi bilgisayarımda "
    "komut çalıştırma veya test etme şansım yok; sunucuyu, derlemeyi ve uçtan uca "
    "testleri doğrudan kendi ortamında bizzat çalıştırıp sonuçlarını doğrula."
)

MAX_SYSTEM_PROMPT = (
    "Sen deneyimli bir Ürün Sahibi ve Baş Yazılım Mimarısın. Karşında tam yetkili "
    "bir geliştirme ortamına (dosya yazma, terminal/bash çalıştırma, arka plan "
    "sunucusu başlatma, test koşma) sahip bir Kodlama Ajanı var ve bu projeyi "
    "onunla birlikte yürütüyorsun.\n\n"
    "KESİN KİMLİK VE ÜSLUP KURALLARI:\n"
    "1. Karşındaki kodlama ajanıyla tamamen doğal, net ve kararlı bir insan proje "
    "sahibi gibi konuş (Türkçe).\n"
    '2. ASLA "Ben bir yapay zekayım", "dil modeliyim", "sandbox\'um yok", '
    '"kod çalıştıramam" gibi ifadeler KULLANMA.\n'
    "3. Şu an bilgisayar başında değilsin / ortamın müsait değil. Kodlama ajanı "
    "senden bir komutu çalıştırmanı, tarayıcıda denemeni veya bir şey kurmanı "
    'isterse doğal biçimde: "Şu an müsait değilim / kendi ortamımda deneyemem, '
    "tüm çalışma ortamı sende; doğrudan kendi ortamında çalıştırıp test et ve "
    'çıktısını bana raporla" de.\n'
    "4. Ajana görsel/resim üretimi, seslendirme veya dış API anahtarı gerektiren "
    "imkânsız görevler VERME; odak tamamen kendi içinde tam çalışan, etkileşimli "
    "web sitesi / yazılım uygulamasıdır.\n"
    "5. Ajana soru/seçim penceresi (ask_user vb.) açtırma; seçenek sormak yerine "
    "en doğru tasarımı uygulamasını iste.\n\n"
    "KALİTE VE DÖNGÜ YÖNETİMİ İLKELERİ (ÇOK ÖNEMLİ):\n"
    '1. LAF KALABALIĞINA KANMA: Ajan metinde "15 özellik ekledim, harika arayüz '
    'kurdum, her şey kusursuz" diye uzun ve süslü bir rapor yazabilir. Metnin '
    "uzunluğu kalite değildir! Sana verilen [SANDBOX GERÇEK EYLEM KANITI] "
    "bölümüne bak:\n"
    "   - Ajan gerçekten dosyaları yazdı mı, derleme/test/uçtan uca doğrulama "
    "komutlarını çalıştırıp exit_code=0 aldı mı?\n"
    "   - Uygulamada tıklayınca çalışmayan sahte butonlar, içi boş sekmeler, "
    "sabit uydurma metinler (placeholder/TODO) kalmış olabilir mi?\n"
    "2. SADECE İSKELETTE BIRAKMA AMA SONSUZ DÖNGÜYE DE GİRME:\n"
    '   - İlk çalışır iskelette hemen "bitti" deyip yüzeysel bırakma; ürünün '
    "kullanıcı için gerçekten anlamlı, yaratıcı, akışı tam ve özenli olmasını sağla.\n"
    "   - ANCAK ajan bir özelliği gerçekten uygulayıp sandbox kanıtıyla "
    "(test/build/HTTP kontrolü exit_code=0) doğruladıysa, sırf emin olmak için "
    "aynı testi tekrar ettirme veya sürekli yeni yan özellikler icat edip projeyi "
    "dağıtma!\n"
    "   - Anlamadığın veya güvenmediğin somut bir nokta varsa hedefli ve tek "
    "seferlik gerçek doğrulama yaptır; doğrulandıysa bir sonraki aşamaya geç.\n"
    "3. DÖRT AŞAMALI YÜRÜTME KAPISI:\n"
    "   - 1_KURGU_ISKELET: Net, özgün ürün kurgusu + uçtan uca çalışan temel "
    "mimari (dağınık 20 yarım özellik değil, odaklı ve tam çalışan çekirdek deneyim).\n"
    "   - 2_GERCEK_ISLEV: Tüm etkileşimlerin, veri akışının, durum yönetiminin, "
    "hata/boş ekran kontrollerinin ve kullanıcıya değer katan yaratıcı detayların "
    "eksiksiz çalışır hâle getirilmesi (sıfır sahte/boş buton).\n"
    "   - 3_TEST_VE_CILA: Uygulamanın sunucu/test betikleriyle gerçek kullanıcı "
    "senaryolarında bizzat çalıştırılıp kırık kenarların onarılması.\n"
    "   - 4_KABUL: Ürün gerçekten anlamlı, bütünlüklü ve çalışma kanıtı tamsa "
    "<DURUM>TAMAMLANDI</DURUM> ver ve döngüyü bitir.\n\n"
    "ÇIKTI BİÇİMİ (HER TURDA TAM OLARAK BU 5 ETİKETİ VER, BAŞKA METİN EKLEME):\n"
    "<DURUM>DEVAM veya TAMAMLANDI veya TAKILDI</DURUM>\n"
    "<ASAMA>1_KURGU_ISKELET veya 2_GERCEK_ISLEV veya 3_TEST_VE_CILA veya 4_KABUL</ASAMA>\n"
    "<DEGERLENDIRME>\n"
    "Ajanın bu turda yaptığı işin gerçek kalitesi, laf kalabalığı mı yoksa somut "
    "çalışan ilerleme mi olduğu ve neden bu kararı verdiğinin kısa analizi.\n"
    "</DEGERLENDIRME>\n"
    "<GUNCEL_HAFIZA>\n"
    "(ÖNEMLİ: Senin sohbet geçmişin yok; bir sonraki turda YALNIZCA buraya yazdığın "
    "hafızayı okuyacaksın! Projenin seçilen mimarisini, tamamlanıp doğrulanan "
    "parçaları ve kalan net maddeleri özlü maddeler hâlinde buraya yaz.)\n"
    "</GUNCEL_HAFIZA>\n"
    "<AJANA_MESAJ>\n"
    "(Doğrudan kodlama ajanına gönderilecek doğal, net, yapıcı ve odaklı mesaj. "
    "DURUM=TAMAMLANDI ise boş bırakabilirsin.)\n"
    "</AJANA_MESAJ>"
)


@dataclass(slots=True)
class TurnDigest:
    """Önceki turların 1-2 satırlık izi (tekrarları önlemek için)."""

    turn: int
    phase: str
    max_summary: str
    agent_tools_summary: str


@dataclass(slots=True)
class MaxDecision:
    """`max`'ın tek turluk yanıtından ayrıştırılan yapısal karar."""

    status: str  # "DEVAM" | "TAMAMLANDI" | "TAKILDI"
    phase: str
    evaluation: str
    updated_memory: str
    message_to_agent: str
    raw_response: str


def build_kickoff_user_prompt(*, project_idea: str, max_turns: int) -> str:
    """1. turda (henüz ajana proje anlatılmamışken) `max`'a verilen başlangıç paketi."""
    return (
        f"[PROJE FİKRİ VE ANA HEDEF]\n{project_idea.strip()}\n\n"
        f"[TUR BİLGİSİ]\nTur: 1 / Azami {max_turns} (Proje Başlangıcı — İlk Talimat)\n\n"
        "[MEVCUT DURUM]\n"
        "Kodlama ajanıyla sohbet yeni açıldı ve geliştirme ortamı (sandbox) hazır bekliyor. "
        "Henüz proje tarif edilmedi.\n\n"
        "[GÖREVİN]\n"
        "Bu proje fikrini sıradan, içi boş bir şablon olmaktan çıkarıp kullanıcının "
        "gerçekten severek kullanacağı anlamlı, yaratıcı ve kendi içinde tutarlı bir "
        "web sitesi/uygulamaya dönüştürecek net bir ürün vizyonu ve 1. aşama "
        "(1_KURGU_ISKELET) başlangıç talimatı hazırla.\n"
        "- Ajandan önce mevcut çalışma dizinini kontrol etmesini, ardından çekirdek "
        "mimarisi ve temel kullanıcı akışı gerçekten çalışan ilk sürümü kurup bizzat "
        "çalıştırarak doğrulamasını iste.\n"
        "- Kağıt üstünde 20 yarım özellik isteme; az sayıda ama derinliği olan, "
        "gerçekten çalışan ve özenli bir ürün iste.\n"
        "- Yanıtını tam olarak <DURUM>, <ASAMA>, <DEGERLENDIRME>, <GUNCEL_HAFIZA> ve "
        "<AJANA_MESAJ> etiketleriyle üret (<DURUM>DEVAM</DURUM> olmalı)."
    )


def build_turn_user_prompt(
    *,
    project_idea: str,
    turn: int,
    max_turns: int,
    current_phase: str,
    memory_summary: str,
    turn_digests: list[TurnDigest],
    last_sent_to_agent: str,
    agent_reply_text: str,
    sandbox_evidence: str,
    anti_loop_warning: str = "",
) -> str:
    """2..N turlarında durumsuz `max`'a verilen tam bağlam paketi."""
    remaining = max(0, max_turns - turn)
    budget_note = ""
    if remaining == 0:
        budget_note = (
            "\n⚠️ SON TURDAYIZ: Bu son değerlendirme hakkındır. Eğer uygulama uçtan uca "
            "çalışır durumdaysa <DURUM>TAMAMLANDI</DURUM> ver; yalnız kritik bir kırık "
            "varsa tek ve son bir düzeltme mesajı yaz."
        )
    elif remaining == 1:
        budget_note = (
            "\n⚠️ SONDAN BİR ÖNCEKİ TUR: Yeni özellik açma! Yalnız mevcut özelliklerin "
            "eksiksiz çalışmasını, uçtan uca test edilmesini ve son cilasını tamamlat."
        )

    history_lines: list[str] = []
    for item in turn_digests[-6:]:
        history_lines.append(
            f"- Tur {item.turn} [{item.phase}]: {item.max_summary} "
            f"(Ajan kanıtı: {item.agent_tools_summary})"
        )
    history_block = "\n".join(history_lines) if history_lines else "(Henüz önceki tur özeti yok)"

    agent_text = agent_reply_text.strip()
    if len(agent_text) > 12000:
        agent_text = (
            agent_text[:6000]
            + "\n\n...[orta kısım kısaltıldı]...\n\n"
            + agent_text[-5500:]
        )

    sections = [
        f"[PROJE FİKRİ VE ANA HEDEF]\n{project_idea.strip()}",
        f"[TUR VE AŞAMA BİLGİSİ]\nTur: {turn} / Azami {max_turns} "
        f"(Kalan tur hakkı: {remaining}) · Önceki Aşama: {current_phase}{budget_note}",
        f"[PROJE HAFIZA DEFTERİ (ÖNCEKİ TURLARDAN SENİN NOTLARIN)]\n"
        f"{memory_summary.strip() or '(Boş)'}",
        f"[KRONOLOJİK TUR İZİ (TEKRARI ÖNLEMEK İÇİN)]\n{history_block}",
        f"[BİR ÖNCEKİ TURDA AJANA GÖNDERDİĞİN MESAJ]\n{last_sent_to_agent.strip()}",
        f"[SANDBOX GERÇEK EYLEM KANITI (OBJEKTİF KAYIT)]\n{sandbox_evidence.strip()}",
        f"[KODLAMA AJANININ SON YANITI (METİN)]\n{agent_text or '(Ajan metin üretmedi)'}",
    ]
    if anti_loop_warning.strip():
        sections.append(f"[DÖNGÜ KORUMA UYARISI]\n{anti_loop_warning.strip()}")

    sections.append(
        "[KARAR GÖREVİN]\n"
        "Yukarıdaki Sandbox Kanıtını ve ajanın yanıtını birlikte değerlendir:\n"
        "1. Ajan gerçekten isteneni kodlayıp çalıştırarak doğruladı mı, yoksa yalnız "
        "metinde süslü bir özet mi geçti?\n"
        "2. Ürün kullanıcı için gerçekten anlamlı, yaratıcı, sahte/boş parça içermeyen "
        "ve uçtan uca test edilmiş bir bütünlüğe ulaştıysa gereksiz tur harcama: "
        "<DURUM>TAMAMLANDI</DURUM> ve <ASAMA>4_KABUL</ASAMA> ver.\n"
        "3. Devam etmesi gerekiyorsa <DURUM>DEVAM</DURUM> ver, <GUNCEL_HAFIZA> alanını "
        "eksiksiz güncelle ve <AJANA_MESAJ> içine tekrar düşmeyen, somut bir sonraki "
        "adımı doğal ürün sahibi üslubuyla yaz."
    )
    return "\n\n".join(sections)


def _extract_tag(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}\s*>(.*?)</{tag}\s*>", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def parse_max_decision(raw_response: str, *, fallback_memory: str = "") -> MaxDecision:
    """`max`'ın etiketli yanıtını ayrıştırır; eksik etiketlerde güvenli yedek uygular."""
    text = (raw_response or "").strip()

    raw_status = _extract_tag(text, "DURUM").upper()
    if "TAMAM" in raw_status or "BITTI" in raw_status or "DONE" in raw_status:
        status = "TAMAMLANDI"
    elif "TAKIL" in raw_status or "BLOCK" in raw_status or "HATA" in raw_status:
        status = "TAKILDI"
    else:
        status = "DEVAM"

    phase = _extract_tag(text, "ASAMA").upper() or (
        "4_KABUL" if status == "TAMAMLANDI" else "2_GERCEK_ISLEV"
    )
    evaluation = _extract_tag(text, "DEGERLENDIRME")
    updated_memory = _extract_tag(text, "GUNCEL_HAFIZA") or fallback_memory
    message_to_agent = _extract_tag(text, "AJANA_MESAJ")

    # Etiket kullanılmadan düz metin döndüyse (nadir): tüm metni ajana mesaj kabul et
    if not message_to_agent and status == "DEVAM":
        # Etiket kalıntılarını temizle
        cleaned = re.sub(r"</?[A-Z_]+\s*>", "", text).strip()
        message_to_agent = cleaned

    if status == "DEVAM" and not message_to_agent.strip():
        status = "TAKILDI"
        evaluation = (evaluation + " | AJANA_MESAJ boş üretildi.").strip(" |")

    return MaxDecision(
        status=status,
        phase=phase,
        evaluation=evaluation,
        updated_memory=updated_memory,
        message_to_agent=message_to_agent.strip(),
        raw_response=text,
    )


def similarity_ratio(a: str, b: str) -> float:
    """İki talimat metni arasındaki normalize benzerlik oranı (`0.0` - `1.0`)."""
    na = " ".join((a or "").lower().split())
    nb = " ".join((b or "").lower().split())
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()
