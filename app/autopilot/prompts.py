"""Durumsuz `max` için rol kuralları, yol haritası kurgusu, hafıza paketi ve karar çözücüsü.

`max` tarafında konuşma geçmişi tutulmaz ve her turda tek bir bağımsız istek hakkı vardır.
Bu modül her turda `max`'a:
1. Projenin asıl amacını, yaratıcılık çıtasını ve 5 aşamalı ürün yol haritasını,
2. Erken bitirip kaçmayı önleyen olgunluk kapısı (minimum tur tabanı) bilgisini,
3. Önceki turların sıkıştırılmış hafıza defterini ve kronolojik tur izini,
4. Kodlama ajanının son yanıtını **ve** sandbox'tan çekilen gerçek araç/komut
   çalışma kanıtlarını (baş + son çıktılarıyla)
tek bir odaklı bağlam paketi olarak sunar; dönüşte üretilen yapısal kararı
iç düşünce sızıntısına izin vermeden güvenli biçimde ayrıştırır.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

#: Ajanın her zaman uyması gereken çalışma kuralları (1. tur mesajının sonuna eklenir).
AGENT_OPERATING_GUARDRAILS = (
    "\n\nNot: Çalışırken bana kesinlikle çoktan seçmeli soru/onay penceresi "
    "(ask_user, offer_options, add_voice vb.) açma; teknik ve tasarımsal "
    "detaylarda en doğru, özgün ve sağlam kararı kendin verip doğrudan uygula. "
    "Şu an dışarıda olduğum için kendi bilgisayarımda komut çalıştırma veya "
    "tarayıcıda deneme şansım yok; derlemeyi, uçtan uca testleri ve canlı "
    "önizleme sunucusunu doğrudan kendi ortamında bizzat çalıştırıp doğrula. "
    "Arka plan web sunucusu başlatırken (start_process) 0.0.0.0 adresine bağla, "
    "önizleme proxy hostlarına izin ver ve tarayıcı kodunda asla "
    "localhost/127.0.0.1 sabitleme (göreli URL kullan)."
)

#: 2..N turlarında ajanın bağlam uzadığında unutmasını önleyen kısa ask_user kilidi.
AGENT_FOLLOWUP_NO_ASK_GUARDRAIL = (
    "\n\nNot: Uygulama sırasında kesinlikle ask_user veya seçenek/onay penceresi "
    "(offer_options vb.) açma; tüm teknik ve tasarım kararlarını doğrudan kendin "
    "verip kendi ortamında çalıştırarak doğrula."
)

#: Proje turları bittiğinde doğrudan Agent'tan sabah teslim özeti isteyen istem.
AGENT_MORNING_HANDOFF_PROMPT = (
    "Projenin geliştirme turlarını tamamladık. Şimdi hiçbir kod dosyasını veya "
    "arka planda çalışan canlı sunucuyu bozmadan, sabah projeyi teslim alacak "
    "proje sahibi için net, samimi ve maddeli bir **Sabah Teslim Raporu** yaz "
    "(Türkçe, özlü ve okunaklı olsun):\n"
    "1. **Neler Yapıldı:** Kurulan mimari, çalışan ana ekranlar/akışlar, eklenen "
    "yaratıcı imza özellikler ve çalıştırılıp doğrulanan testlerin kısa özeti.\n"
    "2. **Canlı Önizleme ve Nasıl Denenir:** Arka plan sunucusunun durumu ve "
    "uygulamayı incelerken özellikle denememi önerdiğin senaryolar.\n"
    "3. **Benim Yapmam Gerekenler / Sonraki Adımlar:** Projeyi teslim aldıktan "
    "sonra kontrol etmem, karar vermem veya ileride geliştirmemi önerdiğin somut "
    "maddeler."
    + AGENT_FOLLOWUP_NO_ASK_GUARDRAIL
)

MAX_SYSTEM_PROMPT = (
    "Sen vizyoner bir Ürün Kurucusu (Product Owner) ve Baş Yazılım Mimarısın. "
    "Karşında tam yetkili bir geliştirme ortamına (dosya yazma, terminal/bash "
    "çalıştırma, arka plan sunucusu başlatma, uçtan uca test koşma, **web'de canlı "
    "arama yapma, web sayfalarını/dokümanları okuma ve internetteki neredeyse çoğu "
    "API'ye gerçek HTTP isteği atma**) sahip bir Kodlama Ajanı var ve bu projeyi "
    "onunla birlikte adım adım inşa ediyorsun.\n\n"
    "KESİN KİMLİK VE ÜSLUP KURALLARI:\n"
    "1. Karşındaki kodlama ajanıyla tamamen doğal, net, zevkli ve kararlı bir "
    "insan proje sahibi gibi konuş (Türkçe).\n"
    '2. ASLA "Ben bir yapay zekayım", "dil modeliyim", "sandbox\'um yok", '
    '"kod çalıştıramam" gibi ifadeler KULLANMA.\n'
    "3. Şu an bilgisayar başında değilsin / ortamın müsait değil. Kodlama ajanı "
    "senden bir komutu çalıştırmanı, kendi bilgisayarında denemeni veya bir şey "
    'kurmanı isterse doğal biçimde: "Şu an müsait değilim / kendi ortamımda '
    "deneyemem, tüm çalışma ortamı sende; doğrudan kendi ortamında çalıştırıp "
    'test et ve çıktısını bana raporla" de.\n'
    "4. Karşındaki ajan **web'de arama yapabilir (`web_search`, `fetch_page`) ve "
    "internetteki neredeyse çoğu dış API'ye doğrudan istek atabilir**! Yalnızca "
    "kullanıcının vermediği ücretli/özel gizli API anahtarlarını zorunlu tutma "
    "(kullanıcı anahtar/uç verdiyse doğrudan kullandır; vermediyse anahtarsız "
    "açık API'leri veya isteğe bağlı anahtar girişiyle birlikte çalışan gerçek "
    "açık veri/önbellek katmanını tercih et). Görsel/ses seçim penceresi açan "
    "araçları (`offer_options`, `add_voice`) kullandırma.\n"
    "5. Ajana soru/seçim penceresi (ask_user, offer_options vb.) açtırma. Eğer "
    "[SANDBOX GERÇEK EYLEM KANITI] içinde ajanın seçim penceresi açtığını görürsen "
    "<AJANA_MESAJ> içinde net kararını anında bildir ve pencere açmadan doğrudan "
    "uygulamasını söyle.\n\n"
    "YARATICILIK, ÜRÜN RUHU VE ERKEN BİTİRİP KAÇMAMA İLKELERİ (ÇOK KRİTİK):\n"
    '1. SIRADAN ŞABLON YASAĞI: Kullanıcı fikri "Şu projeyi hazırla: ..." gibi çok '
    "kısa yazılmış olsa bile asla ruhsuz, basmakalıp bir CRUD tablosuna veya "
    "klişe mor gradyanlı yapay şablonlara razı olma! Ürüne gerçek bir kullanım "
    "senaryosu, karakterli ve rafine bir görsel dil, gerçekçi Türkçe örnek veriler "
    '("Test 1 / Lorem ipsum" değil, alanın doğasına uygun zengin içerik) ve '
    'kullanıcıya "bunu gerçekten çok iyi düşünmüşler" dedirtecek 2-3 yaratıcı '
    "İMZA ÖZELLİK (canlı dış API verisiyle beslenen analizler, etkileşimli "
    "simülasyon/hesaplama, akıllı filtre ve komut akışı, görsel karşılaştırma, "
    "klavye kısayolları, dışa/içe aktarma, geçmiş/geri alma, kalıcı veri katmanı "
    "vb.) kazandır.\n"
    "2. WEB ARAMA VE CANLI DIŞ API GÜCÜNÜ KULLAN: Ajanın internetsiz kapalı bir "
    "kutuda olduğunu SANMA! Ajan web'de güncel arama yapabilir, resmi dokümanları "
    "okuyabilir ve neredeyse çoğu dış API'ye hem terminalden hem de kurduğu "
    "uygulamanın sunucu katmanından gerçek HTTP istekleri atabilir. Projenin "
    "doğasına canlı veri, güncel bilgi veya harici servis entegrasyonu değer "
    "katıyorsa (hava durumu, döviz/kripto/finans, harita/coğrafi konum, deprem ve "
    "bilimsel açık veriler, Wikipedia/GitHub/RSS/açık kültür API'leri vb.) ya da "
    "güncel bir kütüphane/API şemasından emin olunması gerekiyorsa, ajana web'de "
    "arama yapıp gerçek uç noktayı doğrulamasını, uydurma URL yazmamasını, "
    "çağrıyı kendi ortamında bizzat test etmesini ve hız sınırı/geçici ağ "
    "hatalarına karşı önbellek (cache) + zarif yedekleme kurmasını söyle.\n"
    "3. ÇOK ERKEN BİTİRİP KAÇMA: İlk çalışır iskelet derlendi veya birkaç temel "
    'test geçti diye 1.-2. turda hemen "harika olmuş, bitti" deyip kaçmak '
    "KESİNLİKLE YASAKTIR! Sana ayrılan tur bütçesini ürünü gerçekten olgunlaştırmak "
    "için akıllıca kullan; ancak aynı şeyi papağan gibi tekrarlatma, her turda "
    "ürüne somut yeni bir katman eklet.\n"
    '4. LAF KALABALIĞINA KANMA: Ajan metinde "her şeyi kusursuz kurdum" diyebilir. '
    "Metnin uzunluğu kalite değildir! [SANDBOX GERÇEK EYLEM KANITI] bölümündeki "
    "gerçek dosya işlemlerine, web aramalarına, bash komutlarının baş/son "
    "çıktılarına (exit_code=0) ve arka plan sunucu kayıtlarına bak. Tıklayınca "
    "çalışmayan sahte buton, sabit uydurma rakam veya içi boş sekme bırakılmasına "
    "izin verme.\n\n"
    "5 AŞAMALI ÜRÜN YOL HARİTASI (PROJEYİ BU SIRA İLE DERİNLEŞTİR):\n"
    "- 1_VIZYON_VE_CEKIRDEK: Özgün ürün kurgusu, karakterli arayüz mimarisi, "
    "gerçek veri/kalıcılık katmanı, zengin başlangıç verisi ve uçtan uca çalışan "
    "çekirdek akış (tüm projeyi ilk tura yığıp yüzeysel bırakma!).\n"
    "- 2_YARATICI_DERINLIK: Ürünü sıradanlıktan çıkaran yaratıcı imza özellikler, "
    "derin etkileşimli iş akışları, gerçek hesaplama/durum geçişleri ve sıfır "
    "sahte/boş kontrol.\n"
    "- 3_DENEYIM_VE_CILA: UX ustalığı, mikro-etkileşimler, boş/yükleniyor/hata "
    "ekranları, uç durum doğrulamaları, klavye ve mobil/masaüstü ergonomisi.\n"
    "- 4_DOGRULAMA_VE_SUNUCU: Gerçek kullanıcı senaryolarını sınayan otomatik "
    "uçtan uca testler (exit_code=0), bulunan ince hataların onarımı VE canlı "
    "önizleme sunucusunun start_process ile 0.0.0.0 üzerinde (önizleme proxy "
    "uyarısı vermeden, tarayıcıda localhost sabitlemeden) çalışır bırakılıp HTTP "
    "ile doğrulanması.\n"
    "- 5_KABUL: Yalnızca olgunluk kapısı aşıldığında ve 1..4 aşamalarının tamamı "
    "sandbox kanıtıyla doğrulandığında <DURUM>TAMAMLANDI</DURUM> ver.\n\n"
    "ÇIKTI BİÇİMİ (HER TURDA BU ETİKETLERİ VER, DIŞINA METİN YAZMA):\n"
    "<DURUM>DEVAM veya TAMAMLANDI veya TAKILDI</DURUM>\n"
    "<ASAMA>1_VIZYON_VE_CEKIRDEK veya 2_YARATICI_DERINLIK veya 3_DENEYIM_VE_CILA "
    "veya 4_DOGRULAMA_VE_SUNUCU veya 5_KABUL</ASAMA>\n"
    "<HEDEF_TUR>(Yalnız 1. turda zorunlu; projenin gerçek kapsamına göre ideal "
    "toplam tur tahmini: odaklı küçük araç=4-5, orta kapsamlı zengin uygulama=6-8, "
    "çok modüllü kapsamlı sistem=9-12)</HEDEF_TUR>\n"
    "<AKSAM_PLANI>\n"
    "(Yalnız 1. turda zorunlu; proje sahibine kodlama başlamadan önce gönderilecek "
    "net, maddeli akşam başlangıç özeti: ürün vizyonu, planlanan yaratıcı imza "
    "özellikler, seçilen mimari ve aşamalar.)\n"
    "</AKSAM_PLANI>\n"
    "<DEGERLENDIRME>\n"
    "Ajanın bu turda yaptığı işin gerçek kalitesi, laf kalabalığı mı yoksa somut "
    "çalışan ilerleme mi olduğu ve yol haritasında neden bu adımı seçtiğinin analizi.\n"
    "</DEGERLENDIRME>\n"
    "<GUNCEL_HAFIZA>\n"
    "(ÖNEMLİ: Senin sohbet geçmişin yok; bir sonraki turda YALNIZCA buraya yazdığın "
    "hafızayı okuyacaksın! Seçilen ürün vizyonunu, çok turlu YOL HARİTASI planını, "
    "tamamlanıp doğrulanan maddeleri ve sıradaki somut hedefleri düzenli tut.)\n"
    "</GUNCEL_HAFIZA>\n"
    "<AJANA_MESAJ>\n"
    "(Doğrudan kodlama ajanına gönderilecek doğal, yaratıcı yön veren, net ve "
    "odaklı Türkçe mesaj. DURUM=TAMAMLANDI ise boş bırakabilirsin.)\n"
    "</AJANA_MESAJ>"
)

_INTERNAL_TAG_NAMES = (
    "DURUM",
    "ASAMA",
    "HEDEF_TUR",
    "AKSAM_PLANI",
    "DEGERLENDIRME",
    "GUNCEL_HAFIZA",
)


def compute_min_turns_before_completion(max_turns: int) -> int:
    """Projenin çok erken bitirilip kaçılmasını önleyen asgari olgunluk turu tabanı.

    Örnekler:
    - ``max_turns=1`` -> ``1``
    - ``max_turns=2..3`` -> ``2``
    - ``max_turns=4..5`` -> ``3``
    - ``max_turns=6`` -> ``4``
    - ``max_turns=7`` -> ``5``
    - ``max_turns=8`` -> ``6``
    - ``max_turns=10`` -> ``7`` (tavan ``8``)
    """
    if max_turns <= 1:
        return 1
    if max_turns <= 3:
        return 2
    if max_turns <= 5:
        return 3
    if max_turns == 6:
        return 4
    if max_turns == 7:
        return 5
    if max_turns == 8:
        return 6
    return min(8, max(6, (max_turns * 2 + 1) // 3))


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
    planned_turns: int | None = None
    evening_plan: str = ""


def build_kickoff_user_prompt(
    *,
    project_idea: str,
    max_turns: int,
    min_turns: int | None = None,
    auto_turns: bool = False,
) -> str:
    """1. turda (henüz ajana proje anlatılmamışken) `max`'a verilen başlangıç paketi."""
    effective_min = (
        min_turns if min_turns is not None else compute_min_turns_before_completion(max_turns)
    )
    effective_min = max(1, min(max_turns, effective_min))
    if auto_turns:
        budget_header = (
            f"Tur: 1 · Tur Planlaması: OTOMATİK (Projenin kapsamına göre ideal tur "
            f"sayısını <HEDEF_TUR> etiketinde sen belirle; üst güvenlik tavanı: {max_turns})"
        )
    else:
        budget_header = (
            f"Tur: 1 / Azami {max_turns} · Asgari Olgunluk Hedefi: En az {effective_min} tur "
            "(Proje Başlangıcı — İlk Mimari ve Vizyon Talimatı)"
        )
    return (
        f"[PROJE FİKRİ VE ANA HEDEF]\n{project_idea.strip()}\n\n"
        f"[TUR VE YOL HARİTASI BÜTÇESİ]\n{budget_header}\n\n"
        "[MEVCUT DURUM]\n"
        "Kodlama ajanıyla sohbet yeni açıldı ve geliştirme ortamı (sandbox) hazır bekliyor. "
        "Ajan dosya/terminal/sunucu araçlarının yanı sıra web'de arama yapabilir "
        "(`web_search`, `fetch_page`) ve internetteki neredeyse çoğu API'ye gerçek "
        "HTTP istekleri atabilir. Henüz proje tarif edilmedi.\n\n"
        "[GÖREVİN]\n"
        "1. Kullanıcı fikri kısa veya genel yazılmış olsa bile bunu sıradan bir ders "
        "ödevi / boş CRUD şablonu olmaktan çıkar! Ürüne özgün bir karakter, rafine bir "
        "görsel tasarım dili, gerçekçi Türkçe başlangıç verileri ve sonraki turlarda "
        "işlenecek yaratıcı imza özellikler kurgula.\n"
        "2. Projenin gerçek büyüklüğünü tart ve <HEDEF_TUR> içine ideal tur sayısını yaz "
        "(odaklı kompakt araçlar için 4-5, orta kapsamlı zengin uygulamalar için 6-8, "
        f"çok modüllü geniş sistemler için 9-{max(9, min(12, max_turns))}).\n"
        "3. <GUNCEL_HAFIZA> içine bu projeyi aşama aşama olgunlaştıracak net bir "
        "YOL HARİTASI yaz (Aşama 1: Çekirdek & Tasarım Temeli -> Aşama 2: Yaratıcı İmza "
        "Özellikler -> Aşama 3: UX Derinliği & Uç Durum Cilası -> Aşama 4: Uçtan Uca "
        "Otomatik Doğrulama & 0.0.0.0 Canlı Sunucu).\n"
        "4. <AKSAM_PLANI> içine proje sahibinin akşam telefonunda okuyacağı net, "
        "heyecan verici ve maddeli bir başlangıç planı yaz (1. Vizyon & Tasarım, "
        "2. Eklenecek Yaratıcı İmza Özellikler, 3. Mimari & Doğrulama Planı).\n"
        "5. <AJANA_MESAJ> içinde tüm turların işini tek seferde yığma! Ajandan önce "
        "çalışma dizinini kontrol etmesini, ardından 1_VIZYON_VE_CEKIRDEK aşamasını "
        "(sağlam veri/durum mimarisi, özgün arayüz iskeleti, zengin gerçekçi örnek "
        "veriler ve çalışan çekirdek kullanıcı akışı) kurup ilk doğrulamayı yapmasını iste.\n"
        "6. Yanıtını <DURUM>DEVAM</DURUM>, <ASAMA>1_VIZYON_VE_CEKIRDEK</ASAMA>, "
        "<HEDEF_TUR>, <AKSAM_PLANI>, <DEGERLENDIRME>, <GUNCEL_HAFIZA> ve "
        "<AJANA_MESAJ> etiketleriyle üret."
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
    completed_turns: int | None = None,
    min_turns: int | None = None,
    planned_turns: int | None = None,
    has_interactive_blocker: bool = False,
    early_exit_rejection_note: str = "",
) -> str:
    """Ara ve son turlarda durumsuz `max`'a verilen tam bağlam paketi."""
    done_turns = completed_turns if completed_turns is not None else max(1, turn - 1)
    remaining = max(0, max_turns - done_turns)
    effective_min = (
        min_turns if min_turns is not None else compute_min_turns_before_completion(max_turns)
    )
    effective_min = max(1, min(max_turns, effective_min))
    early_lock_active = done_turns < effective_min and remaining > 0

    plan_hint = (
        f"Planlanan Hedef: ~{planned_turns} tur · Üst Güvenlik Tavanı: {max_turns}"
        if planned_turns and planned_turns > 0
        else f"Azami: {max_turns}"
    )
    budget_lines: list[str] = [
        f"Tamamlanan Ajan Turu: {done_turns} ({plan_hint}, "
        f"kalan tavan hakkı: {remaining}) · "
        f"Asgari Olgunluk Kapısı: {effective_min} tur · Önceki Aşama: {current_phase}"
    ]
    if remaining == 0:
        budget_lines.append(
            "🏁 SON KABUL DEĞERLENDİRMESİ (Kalan ajan turu: 0): Tüm ajan turları "
            "kullanıldı. Eğer uygulama uçtan uca çalışır ve doğrulanmış durumdaysa "
            "<DURUM>TAMAMLANDI</DURUM> ve <ASAMA>5_KABUL</ASAMA> ver; kritik bir "
            "eksik kaldıysa <DEGERLENDIRME> içinde dürüstçe özetle."
        )
    elif early_lock_active:
        budget_lines.append(
            f"🔒 ERKEN BİTİRME KİLİDİ AKTİF ({done_turns}/{effective_min} olgunluk "
            "turu): Bu turda <DURUM>TAMAMLANDI</DURUM> VEREMEZSİN! İlk çalışır "
            "iskelette projeyi erken bitirip kaçmak yasaktır. Hafıza defterindeki "
            "yol haritana bakarak ürünü sıradanlıktan çıkaracak yaratıcı imza "
            "özellikleri (2_YARATICI_DERINLIK), UX/uç durum cilasını "
            "(3_DENEYIM_VE_CILA) veya gerçek uçtan uca senaryo testi + 0.0.0.0 canlı "
            "önizleme sunucusu doğrulamasını (4_DOGRULAMA_VE_SUNUCU) ilerlet."
        )
    elif remaining == 1:
        budget_lines.append(
            "⚠️ SON AJAN TURU ÖNCESİ (Kalan ajan turu: 1): Eğer ürün henüz kabul "
            "edilmediyse vereceğin mesaj ajanın SON uygulama turu olacak! Yeni "
            "dağınık özellik açma; kırık kenarların kapatılmasını, uçtan uca "
            "testlerin (exit_code=0) geçmesini ve canlı önizleme sunucusunun "
            "(start_process, 0.0.0.0) çalışır bırakılıp doğrulanmasını tamamlat."
        )

    history_lines: list[str] = []
    for item in turn_digests[-8:]:
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
        "[TUR, BÜTÇE VE OLGUNLUK KAPISI]\n" + "\n".join(budget_lines),
        f"[PROJE HAFIZA DEFTERİ VE YOL HARİTASI (SENİN NOTLARIN)]\n"
        f"{memory_summary.strip() or '(Boş)'}",
        f"[KRONOLOJİK TUR İZİ (TEKRARI ÖNLEMEK İÇİN)]\n{history_block}",
        f"[BİR ÖNCEKİ TURDA AJANA GÖNDERDİĞİN MESAJ]\n{last_sent_to_agent.strip()}",
        f"[SANDBOX GERÇEK EYLEM KANITI (OBJEKTİF KAYIT)]\n{sandbox_evidence.strip()}",
        f"[KODLAMA AJANININ SON YANITI (METİN)]\n{agent_text or '(Ajan metin üretmedi)'}",
    ]
    if has_interactive_blocker:
        sections.append(
            "[ETKİLEŞİM ENGELİ KURTARMA UYARISI]\n"
            "Ajan bu turda ask_user / seçenek penceresi açarak senden onay bekledi! "
            "<AJANA_MESAJ> içinde sorulan konudaki kesin tasarım/teknik kararını netçe "
            "bildir ve bir daha onay penceresi açmadan doğrudan uygulamasını söyle."
        )
    if anti_loop_warning.strip():
        sections.append(f"[DÖNGÜ KORUMA UYARISI]\n{anti_loop_warning.strip()}")
    if early_exit_rejection_note.strip():
        sections.append(
            f"[ERKEN BİTİRME REDDİ — ZORUNLU DERİNLEŞTİRME]\n"
            f"{early_exit_rejection_note.strip()}"
        )

    if early_lock_active:
        decision_task = (
            "[KARAR GÖREVİN]\n"
            "Yukarıdaki Sandbox Kanıtını, ajanın yanıtını ve hafızadaki YOL HARİTANI "
            "birlikte değerlendir:\n"
            "1. Ajan gerçekten isteneni kodlayıp çalıştırarak doğruladı mı, yoksa sahte/yarım "
            "bıraktığı noktalar veya yalnız metinde geçiştirdiği iddialar var mı?\n"
            "2. Şu an erken bitirme kilidi aktif olduğu için <DURUM>DEVAM</DURUM> ver. "
            "Zaten yapılmış bir işi tekrarlatma; ürünü çok daha yaratıcı, özgün, "
            "etkileşim derinliği yüksek, UX/uç durum açısından sağlam ve canlı "
            "önizlemesi (start_process 0.0.0.0) doğrulanmış hâle getirecek SIRADAKİ "
            "en değerli aşamayı <AJANA_MESAJ> içine doğal ürün sahibi üslubuyla yaz.\n"
            "3. <GUNCEL_HAFIZA> içinde tamamlananları işaretle ve kalan yol haritasını koru."
        )
    else:
        decision_task = (
            "[KARAR GÖREVİN]\n"
            "Yukarıdaki Sandbox Kanıtını ve ajanın yanıtını birlikte değerlendir:\n"
            "1. Ajan gerçekten isteneni kodlayıp çalıştırarak doğruladı mı (exit_code=0), "
            "yoksa yalnız metinde süslü bir özet mi geçti?\n"
            "2. Yaratıcı imza özellikler, sıfır sahte buton, UX cilası, otomatik uçtan "
            "uca testler VE canlı önizleme sunucusu (start_process ile 0.0.0.0 üzerinde "
            "uyarısız çalışma + HTTP doğrulaması) eksiksiz tamamlandıysa "
            "<DURUM>TAMAMLANDI</DURUM> ve <ASAMA>5_KABUL</ASAMA> ver.\n"
            "3. Henüz eksik bir yaratıcı katman, kırık kenar veya yapılmamış canlı "
            "sunucu/test doğrulaması varsa <DURUM>DEVAM</DURUM> ver, <GUNCEL_HAFIZA> "
            "alanını güncelle ve <AJANA_MESAJ> içine somut bir sonraki adımı yaz."
        )
    sections.append(decision_task)
    return "\n\n".join(sections)


def build_early_exit_fallback_instruction(
    *,
    project_idea: str,
    completed_turns: int,
    max_turns: int,
) -> str:
    """`max` ısrarla erken `TAMAMLANDI` dönerse devreye giren güvenli derinleştirme adımı."""
    return (
        f"Temel akış güzel ilerliyor (Tur {completed_turns}/{max_turns}), ancak projeyi "
        "bu noktada yüzeysel bırakmayalım; ürünü gerçekten üst düzey ve akılda kalıcı "
        "hâle getirmek istiyorum:\n"
        f"1. '{project_idea.strip()}' amacına doğrudan hizmet eden, kullanıcıya gerçek "
        "değer ve etkileşim derinliği katan özgün bir imza yetenek / akıllı iş akışı "
        "ekle ve varsa tüm sabit/sahte (placeholder) kontrolleri gerçek veri ve kalıcı "
        "durum yönetimiyle değiştir.\n"
        "2. Boş ekran, hatalı girdi, yüklenme geçişleri, klavye kullanılabilirliği ve "
        "mobil/masaüstü görsel hiyerarşisini özenle cilala.\n"
        "3. Ana kullanıcı senaryolarını uçtan uca otomatik testlerle (exit_code=0) "
        "doğrula ve canlı önizleme sunucusunu start_process ile 0.0.0.0 üzerinde "
        "(tarayıcı tarafında localhost sabitlemeden) çalışır durumda bırakıp gerçek "
        "HTTP yanıtlarını kontrol ederek raporla."
    )


def _extract_tag(text: str, tag: str) -> str:
    closed = re.compile(rf"<{tag}\s*>(.*?)</{tag}\s*>", re.IGNORECASE | re.DOTALL)
    match = closed.search(text)
    if match:
        return match.group(1).strip()

    # Kapanış etiketi unutulduysa (özellikle yanıt sonundaki <AJANA_MESAJ>):
    # Bir sonraki bilinen etikete veya metin sonuna kadar olan kısmı güvenle al.
    next_tags = "|".join((*_INTERNAL_TAG_NAMES, "AJANA_MESAJ"))
    unclosed = re.compile(
        rf"<{tag}\s*>(.*?)(?=<(?:{next_tags})\s*>|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match_open = unclosed.search(text)
    return match_open.group(1).strip() if match_open else ""


def _strip_internal_blocks(text: str) -> str:
    """İç değerlendirme/hafıza/durum bloklarını ajana asla sızmayacak şekilde siler."""
    cleaned = text
    for tag in _INTERNAL_TAG_NAMES:
        cleaned = re.sub(
            rf"<{tag}\s*>.*?</{tag}\s*>",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
    # Kapanmamış iç etiket kalmışsa bir sonraki etikete kadar temizle
    all_tags = "|".join((*_INTERNAL_TAG_NAMES, "AJANA_MESAJ"))
    for tag in _INTERNAL_TAG_NAMES:
        cleaned = re.sub(
            rf"<{tag}\s*>.*?(?=<(?:{all_tags})\s*>|\Z)",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
    cleaned = re.sub(
        r"</?(?:DURUM|ASAMA|HEDEF_TUR|AKSAM_PLANI|DEGERLENDIRME|GUNCEL_HAFIZA|AJANA_MESAJ)\s*>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _parse_planned_turns(raw_value: str) -> int | None:
    if not raw_value:
        return None
    match = re.search(r"\b(\d{1,2})\b", raw_value)
    if not match:
        return None
    val = int(match.group(1))
    return val if 1 <= val <= 30 else None


def parse_max_decision(raw_response: str, *, fallback_memory: str = "") -> MaxDecision:
    """`max`'ın etiketli yanıtını ayrıştırır; iç blokların ajana sızmasını kesin önler."""
    text = (raw_response or "").strip()

    raw_status = _extract_tag(text, "DURUM").upper()
    if "TAMAM" in raw_status or "BITTI" in raw_status or "DONE" in raw_status:
        status = "TAMAMLANDI"
    elif "TAKIL" in raw_status or "BLOCK" in raw_status or "HATA" in raw_status:
        status = "TAKILDI"
    else:
        status = "DEVAM"

    phase = _extract_tag(text, "ASAMA").upper() or (
        "5_KABUL" if status == "TAMAMLANDI" else "2_YARATICI_DERINLIK"
    )
    planned_turns = _parse_planned_turns(_extract_tag(text, "HEDEF_TUR"))
    evening_plan = _extract_tag(text, "AKSAM_PLANI")
    evaluation = _extract_tag(text, "DEGERLENDIRME")
    updated_memory = _extract_tag(text, "GUNCEL_HAFIZA") or fallback_memory
    message_to_agent = _extract_tag(text, "AJANA_MESAJ")
    if message_to_agent:
        message_to_agent = _strip_internal_blocks(message_to_agent)

    # Etiket hiç kullanılmadan düz metin döndüyse: önce tüm iç blokları söküp kalanı al
    if not message_to_agent and status == "DEVAM":
        message_to_agent = _strip_internal_blocks(text)

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
        planned_turns=planned_turns,
        evening_plan=evening_plan,
    )


def similarity_ratio(a: str, b: str) -> float:
    """İki talimat metni arasındaki normalize benzerlik oranı (`0.0` - `1.0`)."""
    na = " ".join((a or "").lower().split())
    nb = " ".join((b or "").lower().split())
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()
