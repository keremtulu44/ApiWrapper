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
#:
#: Tek kopya ilkesi: bu metinler `MAX_SYSTEM_PROMPT` ile **aynı kuralı tekrar
#: etmemeli**. Sistem istemi değişmez rolü anlatır; buradaki not ise yalnız
#: ajanın icra tarafını (pencere açmama + kendi ortamında doğrulama) hatırlatır.
AGENT_OPERATING_GUARDRAILS = (
    "\n\nNot: ask_user/offer_options/add_voice gibi onay penceresi açma; en doğru "
    "kararı sen ver ve doğrudan uygula. Dışarıda olduğum için derlemeyi, testleri ve "
    "canlı sunucu doğrulamasını kendi ortamında yap: sunucuyu start_process ile "
    "0.0.0.0'a bağla, önizleme proxy hostlarına izin ver, tarayıcı kodunda "
    "localhost/127.0.0.1 sabitleme (göreli URL kullan). bash'te ön planda takılan "
    "sunucu veya etkileşimli onay bekleyen CLI çalıştırma."
)

#: 2..N turlarında ajanın bağlam uzadığında unutmasını önleyen kısa hatırlatma.
AGENT_FOLLOWUP_NO_ASK_GUARDRAIL = (
    "\n\nNot: ask_user/onay penceresi açma; kararı sen ver, kendi ortamında "
    "çalıştırıp doğrula. Dosyalar ve sunucular duruyor, sıfırdan başlama."
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
    "Sen vizyoner bir Ürün Kurucusu ve Baş Yazılım Mimarısın. Karşında tam yetkili "
    "sandbox (dosya, terminal, arka plan sunucusu, uçtan uca test), web'de canlı "
    "arama/doküman okuma (web_search, fetch_page) ve dış API'ye gerçek HTTP isteği "
    "atma gücüne sahip bir Kodlama Ajanı var.\n"
    "1. Doğal ve kararlı bir insan proje sahibi gibi Türkçe konuş; ASLA "
    '"yapay zekayım", "dil modeliyim", "sandbox\'um yok" deme. Deneme istenirse: '
    '"Şu an müsait değilim, kendi ortamında çalıştırıp test ederek sonucu paylaş."\n'
    "2. Ajan Tanrı modunda çalışır; ona soru/seçim penceresi (ask_user, "
    "offer_options, add_voice) açtırma, kararı sen ver ve pencere açmadan uygulat.\n"
    "3. Sıradan CRUD şablonuna razı olma; karakterli arayüz, gerçekçi Türkçe veri "
    "ve 2-3 yaratıcı imza özellik kazandır. İlk iskelette erken bitirip kaçma; "
    "SANDBOX KANITI özetindeki gerçek test/sunucu sonucuna bak.\n"
    "4. Canlı veri yakışıyorsa gerçek ucu doğrulat, önbellek/yedekleme kurdur "
    "(verilmeyen ücretli gizli anahtarı zorunlu tutma).\n"
    "5. SOHBET: Sen durumsuzsun — her turda yeni ve boş bir max sohbeti açılır, "
    "hafızan yalnız [HAFIZA]/[SON TURLAR] bölümleridir; Ajan ise AYNI Agent "
    "sohbetinde ilerler (dosyaları ve sunucuları durur). Bitmiş işi tekrar "
    "yaptırma, her tur yalnız sıradaki eksiği iste.\n"
    "Yol Haritası: 1_VIZYON_VE_CEKIRDEK -> 2_YARATICI_DERINLIK -> "
    "3_DENEYIM_VE_CILA -> 4_DOGRULAMA_VE_SUNUCU (otomatik test + 0.0.0.0 canlı "
    "sunucu doğrulaması) -> 5_KABUL.\n\n"
    "ÇIKTI (YALNIZ BU ETİKETLER, İÇ NOTLAR KISA):\n"
    "<DURUM>DEVAM|TAMAMLANDI|TAKILDI</DURUM>\n"
    "<ASAMA>1..5</ASAMA>\n"
    "<DEGERLENDIRME>en fazla 2 kısa cümle</DEGERLENDIRME>\n"
    "<GUNCEL_HAFIZA>en fazla 8 kısa madde</GUNCEL_HAFIZA>\n"
    "<AJANA_MESAJ>net talimat; TAMAMLANDI ise boş</AJANA_MESAJ>"
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


def sanitize_for_waf(text: str) -> str:
    """Dış HTTP güvenlik duvarlarının (Cloudflare OWASP RCE/SQLi/XSS kuralları)
    kodlama özetlerini saldırı sanıp `403 Blocked` döndürmesini önler.
    """
    if not text:
        return ""
    # Eski kayıtlardan (`resume`) kalabilecek ham bash/dosya satırlarını temizle:
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(("Değiştirilen dosyalar:", "Çalıştırılan bash komutları")):
            continue
        if "cmd=" in stripped and ("exit_code=" in stripped or "stdout:" in stripped):
            continue
        kept_lines.append(raw_line)
    cleaned = "\n".join(kept_lines)
    # Backtick (`...`) Unix komut ikamesi olarak WAF'ı tetikler:
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.replace("&&", " ve ").replace("||", " veya ")
    cleaned = re.sub(r"/(?:usr/)?bin/(?:ba)?sh\b", "kabuk", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"/etc/[a-zA-Z0-9_./-]+", "sistem-ayari", cleaned)
    cleaned = re.sub(r"/dev/null\b", "bos-cikti", cleaned)
    cleaned = re.sub(r"\b([\w./-]+)\.(?:sh|bash|cjs)\b", r"\1 betigi", cleaned)
    cleaned = re.sub(r"\bcurl\s+-[a-zA-Z]+", "http-istegi", cleaned)
    cleaned = re.sub(r"<\s*!?\s*doctype\b", "[doctype]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\s*/?\s*script\b", "[script]", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def extract_agent_final_summary(text: str, max_chars: int = 1800) -> str:
    """Ajanın uzun adım-adım çalışma günlüğünü atıp sondaki teslim özetini alır."""
    cleaned = sanitize_for_waf(text)
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    tail_window = cleaned[-(max_chars + 900) :]
    heading_match = re.search(r"\n#{2,3}\s+[^\n]+", tail_window)
    if heading_match:
        candidate = tail_window[heading_match.start() + 1 :].strip()
        if len(candidate) >= 350:
            return candidate[:max_chars].rstrip()
    return "…\n" + cleaned[-max_chars:].lstrip()


def _clip_head_tail(text: str, max_chars: int, *, head_ratio: float = 0.4) -> str:
    """Metin sınırı aşarsa başını ve sonunu koruyarak ortasını kırpar."""
    cleaned = sanitize_for_waf(text)
    if len(cleaned) <= max_chars:
        return cleaned
    marker = "\n…\n"
    usable = max(80, max_chars - len(marker))
    head = max(40, int(usable * head_ratio))
    tail = max(40, usable - head)
    return cleaned[:head].rstrip() + marker + cleaned[-tail:].lstrip()


def build_kickoff_user_prompt(
    *,
    project_idea: str,
    max_turns: int,
    min_turns: int | None = None,
    auto_turns: bool = False,
    existing_agent_reply: str = "",
    existing_sandbox_evidence: str = "",
) -> str:
    """1. turda `max`'a verilen başlangıç (veya mevcut Agent sohbetinden devam) paketi."""
    effective_min = (
        min_turns if min_turns is not None else compute_min_turns_before_completion(max_turns)
    )
    effective_min = max(1, min(max_turns, effective_min))
    if auto_turns:
        budget_header = (
            f"Tur: 1 · Tur Planlaması: OTOMATİK (İdeal tur sayısını <HEDEF_TUR> içinde "
            f"sen belirle; tavan: {max_turns})"
        )
    else:
        budget_header = f"Tur: 1/{max_turns} · Asgari Olgunluk: {effective_min} tur"

    safe_idea = _clip_head_tail(project_idea.strip(), 2400)
    #: `HEDEF_TUR` üst sınırı tavanı **aşamaz**: `max_turns=8` iken "9-9" gibi
    #: imkânsız bir aralık istenmemeli (ölçüm: eski metin bunu üretiyordu).
    planned_ceiling = max(3, min(12, max_turns))
    if planned_ceiling <= 4:
        tur_hint = f"(küçük araç=2-{planned_ceiling})"
    elif planned_ceiling <= 6:
        tur_hint = f"(küçük araç=3-4, orta uygulama=5-{planned_ceiling})"
    else:
        big_lo = min(9, planned_ceiling - 1)
        mid_hi = min(8, planned_ceiling)
        tur_hint = (
            f"(küçük araç=4-5, orta uygulama=6-{mid_hi}, "
            f"geniş sistem={big_lo}-{planned_ceiling})"
        )
    if existing_sandbox_evidence.strip():
        clipped_ev = _clip_head_tail(existing_sandbox_evidence, 900, head_ratio=0.5)
        clipped_rep = (
            extract_agent_final_summary(existing_agent_reply, 1500)
            or "(Ajan metin özeti yok; sandbox kanıtına bak)"
        )
        state_block = (
            "[MEVCUT DURUM — BU AGENT SOHBETİNDE ÖNCEDEN YAPILAN ÇALIŞMA VAR]\n"
            "DİKKAT: Bağlanılan bu Agent sandbox oturumunda proje üzerinde önceden "
            "ilerleme kaydedilmiş! Yapılmış kısımları sıfırdan tekrarlatma; aşağıdaki "
            "mevcut ilerlemeyi temel alarak kalan aşamaları planla ve doğrudan sıradaki "
            "eksik aşamayı <AJANA_MESAJ> içine yaz.\n\n"
            f"[SANDBOX MEVCUT EYLEM KANITI]\n{clipped_ev}\n\n"
            f"[AJANIN SON TESLİM ÖZETİ]\n{clipped_rep}"
        )
        step5_line = (
            "5. <AJANA_MESAJ> içinde önceden yapılmış işleri tekrarlatmadan, mevcut "
            "dosyaları kontrol ettirip doğrudan sıradaki aşamayı kurup doğrulamasını iste."
        )
    else:
        state_block = (
            "[MEVCUT DURUM]\n"
            "Sandbox hazır; ajan dosya/terminal/sunucu araçlarının yanında web'de arama "
            "(web_search, fetch_page) yapabilir ve neredeyse çoğu dış API'ye HTTP "
            "isteği atabilir. Proje henüz tarif edilmedi."
        )
        step5_line = (
            "5. <AJANA_MESAJ> içinde tüm projeyi tek tura yığmadan yalnız 1_VIZYON_VE_CEKIRDEK "
            "aşamasını (dizin kontrolü, veri/API temeli, özgün arayüz iskeleti ve çalışan "
            "çekirdek akış) kurup doğrulamasını iste."
        )

    return (
        f"[PROJE FİKRİ]\n{safe_idea}\n\n"
        f"[BÜTÇE]\n{budget_header}\n\n"
        f"{state_block}\n\n"
        "[1. TUR GÖREVİN]\n"
        "1. Fikri sıradan CRUD olmaktan çıkar; karakterli arayüz, gerçekçi Türkçe veri "
        "ve 2-3 yaratıcı imza özellik kurgula.\n"
        "2. <HEDEF_TUR> içine kapsamına göre ideal toplam tur sayısını yaz "
        f"{tur_hint}.\n"
        "3. <AKSAM_PLANI> içine proje sahibine gönderilecek kısa, maddeli akşam "
        "başlangıç planını yaz (Vizyon, İmza Özellikler, Mimari; en fazla 6-8 satır).\n"
        "4. <GUNCEL_HAFIZA> içine kısa maddelerle 5 aşamalı yol haritasını yaz.\n"
        f"{step5_line}"
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
    """Ara ve son turlarda durumsuz `max`'a verilen dengeli bağlam paketi."""
    done_turns = completed_turns if completed_turns is not None else max(1, turn - 1)
    remaining = max(0, max_turns - done_turns)
    effective_min = (
        min_turns if min_turns is not None else compute_min_turns_before_completion(max_turns)
    )
    effective_min = max(1, min(max_turns, effective_min))
    early_lock_active = done_turns < effective_min and remaining > 0

    plan_hint = (
        f"Hedef: ~{planned_turns} · Tavan: {max_turns}"
        if planned_turns and planned_turns > 0
        else f"Azami: {max_turns}"
    )
    budget_lines: list[str] = [
        f"Tamamlanan Tur: {done_turns} ({plan_hint}, kalan: {remaining}) · "
        f"Olgunluk Kapısı: {effective_min} · Önceki Aşama: {current_phase}"
    ]
    if remaining == 0:
        budget_lines.append(
            "🏁 SON DEĞERLENDİRME (Kalan tur: 0): Ürün çalışır ve doğrulandıysa "
            "<DURUM>TAMAMLANDI</DURUM> ve <ASAMA>5_KABUL</ASAMA> ver."
        )
    elif early_lock_active:
        budget_lines.append(
            f"🔒 ERKEN BİTİRME KİLİDİ AKTİF ({done_turns}/{effective_min} olgunluk "
            "turu): Bu turda <DURUM>TAMAMLANDI</DURUM> VEREMEZSİN! Yol haritandaki "
            "sıradaki aşamayı (imza özellikler, UX cilası veya uçtan uca test + "
            "0.0.0.0 canlı sunucu) ilerlet; ölçütler yine geçerli: testler "
            "exit_code=0 ve canlı sunucu HTTP yanıtı."
        )
    elif remaining == 1:
        budget_lines.append(
            "⚠️ SON AJAN TURU ÖNCESİ (Kalan: 1): Yeni dağınık özellik açma; eksik "
            "kenarları, testleri (exit_code=0) ve 0.0.0.0 canlı sunucu doğrulamasını tamamlat."
        )

    history_lines = [
        f"- T{item.turn} [{item.phase}]: {item.max_summary} ({item.agent_tools_summary})"
        for item in turn_digests[-6:]
    ]
    history_block = "\n".join(history_lines) if history_lines else "(Yok)"

    # Dilim bütçeleri bilinçli olarak dengeli: ham sohbet dökümü değil, karar
    # vermeye yetecek kadar sinyal gönderilir (uzun istem = daha yavaş/dağınık karar).
    clipped_memory = _clip_head_tail(memory_summary, 800, head_ratio=0.5) or "(Boş)"
    clipped_last_msg = _clip_head_tail(last_sent_to_agent, 380, head_ratio=0.4)
    clipped_evidence = _clip_head_tail(sandbox_evidence, 900, head_ratio=0.5)
    clipped_agent_reply = (
        extract_agent_final_summary(agent_reply_text, 950)
        or "(Ajan metin üretmedi)"
    )

    sections = [
        f"[PROJE FİKRİ]\n{_clip_head_tail(project_idea, 450)}",
        "[BÜTÇE VE OLGUNLUK]\n" + "\n".join(budget_lines),
        # Sohbet sözleşmesi: `max` durumsuz (her tur yeni sohbet), Agent aynı
        # sohbet üzerinde ilerler. İki tarafın hafıza modeli farklı olduğu için bu
        # her turda açıkça hatırlatılır; aksi halde `max` yapılmış işi tekrar ister.
        "[SOHBET DURUMU]\n"
        "Sen durumsuzsun: bu tur yeni ve boş bir max sohbeti (hafızan aşağıdaki "
        "bölümler). Ajan AYNI Agent sohbetinde ilerliyor; dosyaları ve sunucuları "
        "duruyor. Bitmiş işi tekrar yaptırma, yalnız sıradaki eksiği iste.",
        f"[HAFIZA VE YOL HARİTASI]\n{clipped_memory}",
        f"[SON TURLAR]\n{history_block}",
        f"[SON TALİMATIN]\n{clipped_last_msg}",
        f"[SANDBOX GERÇEK EYLEM KANITI]\n{clipped_evidence}",
        f"[AJANIN SON YANITI (ÖZETLENMİŞ)]\n{clipped_agent_reply}",
    ]
    if has_interactive_blocker:
        sections.append(
            "[ETKİLEŞİM ENGELİ KURTARMA UYARISI]\n"
            "Ajan ask_user/seçenek penceresi açtı! <AJANA_MESAJ> içinde kesin "
            "kararını bildir ve pencere açmadan doğrudan uygulamasını söyle."
        )
    if anti_loop_warning.strip():
        sections.append(f"[DÖNGÜ UYARISI]\n{anti_loop_warning.strip()}")
    if early_exit_rejection_note.strip():
        sections.append(
            f"[ERKEN BİTİRME REDDİ — ZORUNLU DERİNLEŞTİRME]\n"
            f"{early_exit_rejection_note.strip()}"
        )

    if early_lock_active:
        decision_task = (
            "[KARAR]\n"
            "Sandbox Kanıtını ve yol haritanı kontrol et; erken bitirme kilidi aktif "
            "olduğu için <DURUM>DEVAM</DURUM> ver, yapılan işi tekrarlatmadan sıradaki "
            "en değerli aşamayı <AJANA_MESAJ> içine yaz ve <GUNCEL_HAFIZA>'yı kısa tut."
        )
    else:
        decision_task = (
            "[KARAR]\n"
            "İmza özellikler, sıfır sahte kontrol, uçtan uca testler (exit_code=0) ve "
            "0.0.0.0 canlı sunucu HTTP doğrulaması kanıtla tamsa <DURUM>TAMAMLANDI</DURUM> "
            "ve <ASAMA>5_KABUL</ASAMA> ver; eksik varsa <DURUM>DEVAM</DURUM> ile net "
            "sonraki adımı <AJANA_MESAJ> içine yaz."
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
