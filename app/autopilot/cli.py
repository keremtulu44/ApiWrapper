"""Komut satırı arayüzü: `python -m app.autopilot <komut>`.

Alt komutlar:
* ``inspect``     : Ağa çıkmadan Agent (ve varsa Max) cURL'ünü doğrular, iki hesabın
                    ayrı Google hesabı olup olmadığını kontrol eder.
* ``check-agent`` : Agent sohbetine bağlanıp (`?recoverRun=false`) oturumun canlı
                    olduğunu doğrular (mesaj göndermez).
* ``run``         : Yeni bir kendi kendine yürüyen `max` ↔ Agent Mode projesi başlatır.
* ``resume``      : Kaydedilmiş durum dosyasından (`--state-file`) projeyi sürdürür.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Literal

from app.autopilot.agent_client import AgentSessionClient
from app.autopilot.curl_parse import extract_identity_subject, parse_agent_curl
from app.autopilot.events_parser import AgentStreamDelta
from app.autopilot.max_bridge import (
    DirectCurlMaxBridge,
    ExternalHttpMaxBridge,
    MaxBridge,
)
from app.autopilot.orchestrator import (
    AutopilotConfig,
    AutopilotOrchestrator,
    AutopilotRunState,
)
from app.upstream.curl_import import build_settings, parse_curl


def _read_secret_input(
    *,
    file_path: str | None,
    env_name: str,
    label: str,
    required: bool = True,
) -> str:
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise SystemExit(f"HATA: {label} dosyası bulunamadı: {path}")
        return path.read_text(encoding="utf-8-sig").lstrip("\ufeff").strip()
    env_val = os.environ.get(env_name, "").lstrip("\ufeff").strip()
    if env_val:
        return env_val
    if required:
        raise SystemExit(
            f"HATA: {label} verilmedi. Dosya yolu belirtin veya {env_name} ortam "
            "değişkenini ayarlayın."
        )
    return ""


def _read_idea(idea: str | None, idea_file: str | None) -> str:
    if idea and idea.strip():
        return idea.strip()
    if idea_file:
        path = Path(idea_file)
        if not path.is_file():
            raise SystemExit(f"HATA: Proje fikri dosyası bulunamadı: {path}")
        return path.read_text(encoding="utf-8-sig").lstrip("\ufeff").strip()
    raise SystemExit("HATA: --idea veya --idea-file ile proje fikri verilmelidir.")


def _build_max_bridge_from_args(args: argparse.Namespace) -> MaxBridge:
    max_curl = _read_secret_input(
        file_path=getattr(args, "max_curl_file", None),
        env_name="MAX_CURL",
        label="Max cURL",
        required=False,
    )
    if max_curl:
        return DirectCurlMaxBridge(max_curl)

    max_url = (
        getattr(args, "max_url", None)
        or os.environ.get("MAX_BASE_URL")
        or os.environ.get("WRAPPER_BASE_URL")
        or ""
    ).strip()
    max_key = (
        getattr(args, "max_api_key", None)
        or os.environ.get("MAX_API_KEY")
        or os.environ.get("WRAPPER_API_KEY")
        or ""
    ).strip()
    if not max_url or not max_key:
        raise SystemExit(
            "HATA: Max bağlantısı için ya (--max-url + --max-api-key) ya da "
            "(--max-curl-file / MAX_CURL) verilmelidir."
        )
    endpoint_mode: Literal["chat", "ask"] = (
        "ask" if getattr(args, "max_endpoint", "chat") == "ask" else "chat"
    )
    return ExternalHttpMaxBridge(
        max_url,
        max_key,
        model=getattr(args, "max_model", "max"),
        endpoint_mode=endpoint_mode,
    )


def _cmd_inspect(args: argparse.Namespace) -> int:
    raw_agent = _read_secret_input(
        file_path=args.agent_curl_file,
        env_name="AGENT_CURL",
        label="Agent cURL",
    )
    try:
        cfg = parse_agent_curl(raw_agent, chat_id_override=args.chat_id)
    except ValueError as exc:
        print(f"❌ Agent cURL geçersiz: {exc}", file=sys.stderr)
        return 1

    agent_sub = cfg.account_identity()
    print("=== Agent Mode cURL Özeti (gizli değerler gizlendi) ===")
    print(f"  Hedef alan adı      : {cfg.target_domain}")
    print(f"  Kaynak uç nokta     : {cfg.source_endpoint_kind}")
    print(f"  Sohbet kimliği      : {cfg.chat_id}")
    print(f"  Trigger adresi      : {cfg.effective_trigger_address}")
    print(f"  Son kullanıcı msg ID: {cfg.last_user_message_id or '(bağlanınca okunacak)'}")
    print(f"  Oturum çerezi       : {'var' if cfg.cookie else 'yok'}")
    print(f"  recaptchaV3Token    : {'var' if cfg.recaptcha_token else 'YOK'}")
    print(f"  cx                  : {'var' if cfg.cx else 'yok'}")
    print(f"  Hesap kimliği (sub) : {'algılandı' if agent_sub else 'çözülemedi'}")
    if cfg.warnings:
        print("  Uyarılar:")
        for w in cfg.warnings:
            print(f"    ⚠️  {w}")

    raw_max = _read_secret_input(
        file_path=args.max_curl_file,
        env_name="MAX_CURL",
        label="Max cURL",
        required=False,
    )
    if raw_max:
        parsed_max = parse_curl(raw_max)
        env_max, _, model_id = build_settings(parsed_max)
        max_sub = extract_identity_subject(
            env_max.get("UPSTREAM_COOKIE", ""),
            env_max.get("UPSTREAM_ACCESS_TOKEN", ""),
        )
        print("\n=== Max cURL Karşılaştırması ===")
        print(f"  Hedef alan adı      : {env_max.get('TARGET_DOMAIN') or '(yok)'}")
        print(f"  Akış yolu           : {env_max.get('UPSTREAM_STREAM_PATH') or '(yok)'}")
        print(f"  modelAId            : {model_id or '(yok)'}")
        if agent_sub and max_sub:
            if agent_sub == max_sub:
                print(
                    "  ❌ HATA: Agent ve Max cURL'leri AYNI Google hesabına ait! "
                    "Ayrı hesap kullanın."
                )
                return 2
            print("  ✅ İki cURL FARKLI Google hesaplarına ait (güvenli).")
    return 0


async def _cmd_check_agent_async(args: argparse.Namespace) -> int:
    raw_agent = _read_secret_input(
        file_path=args.agent_curl_file,
        env_name="AGENT_CURL",
        label="Agent cURL",
    )
    async with AgentSessionClient.from_curl(raw_agent, chat_id_override=args.chat_id) as client:
        state = await client.fetch_state(recover_run=False)
        print("✅ Agent sohbetine başarıyla bağlanıldı!")
        print(f"  Sohbet ID           : {state.chat_id}")
        print(f"  Trigger adresi      : {state.trigger_address}")
        print(f"  Toplam mesaj sayısı : {len(state.messages)}")
        print(f"  Son event sıra no   : {state.last_event_id}")
        print(f"  Son kullanıcı msg ID: {state.last_user_message_id or '(yok)'}")
        pending_txt = "EVET (pending)" if state.has_pending_turn else "Hayır (hazır)"
        print(f"  Devam eden işlem    : {pending_txt}")
    return 0


async def _run_or_resume_async(args: argparse.Namespace, *, resume: bool) -> int:
    raw_agent = _read_secret_input(
        file_path=args.agent_curl_file,
        env_name="AGENT_CURL",
        label="Agent cURL",
    )
    state_path = Path(args.state_file)
    resume_state: AutopilotRunState | None = None

    if resume:
        if not state_path.is_file():
            raise SystemExit(f"HATA: Durum dosyası bulunamadı: {state_path}")
        loaded = json.loads(state_path.read_text(encoding="utf-8-sig"))
        resume_state = AutopilotRunState.from_dict(loaded)
        idea = resume_state.project_idea
        chat_id_override = args.chat_id or resume_state.chat_id
    else:
        idea = _read_idea(args.idea, args.idea_file)
        chat_id_override = args.chat_id

    agent = AgentSessionClient.from_curl(
        raw_agent,
        chat_id_override=chat_id_override,
        turn_timeout=args.agent_timeout,
    )
    bridge = _build_max_bridge_from_args(args)

    cfg = AutopilotConfig(
        project_idea=idea,
        max_turns=args.max_turns,
        auto_turns=args.auto_turns,
        respect_night_window=args.night_window,
        gap_min_seconds=args.gap_min,
        gap_max_seconds=args.gap_max,
        state_file=state_path,
        run_id=resume_state.run_id if resume_state else f"auto_{agent.config.chat_id[:8]}",
    )

    def _on_progress(_st: AutopilotRunState, note: str) -> None:
        print(f"\n[OTOPİLOT] {note}", flush=True)

    def _on_delta(delta: AgentStreamDelta) -> None:
        if not args.stream_agent:
            return
        if delta.kind == "tool_input" and delta.tool_name:
            print(f"\n  🔧 [Agent Araç: {delta.tool_name}]", flush=True)
        elif delta.kind == "text" and delta.text:
            print(delta.text, end="", flush=True)

    orch = AutopilotOrchestrator(
        config=cfg,
        agent=agent,
        max_bridge=bridge,
        on_progress=_on_progress,
        on_agent_delta=_on_delta,
        resume_state=resume_state,
    )

    final_state = await orch.run()
    print("\n" + "=" * 64)
    print(f"Sonuç Durumu : {final_state.status}")
    print(f"Tamamlanan   : {len(final_state.turns)} / {final_state.max_turns} tur")
    print(f"Durum Dosyası: {state_path}")
    if final_state.stop_reason:
        print(f"Açıklama     : {final_state.stop_reason}")
    if final_state.handoff_report:
        print("\n--- Agent Teslim Özeti ---\n" + final_state.handoff_report)
    print("=" * 64)
    return 0 if final_state.status in {"completed", "max_turns_reached", "stopped"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.autopilot",
        description="Durumsuz Max ↔ Sandbox'lu Agent Mode kendi kendine yürüyen proje aracı.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_ins = sub.add_parser(
        "inspect", help="Agent (ve Max) cURL dosyasını ağa çıkmadan doğrular."
    )
    p_ins.add_argument(
        "--agent-curl-file", help="Agent Mode cURL metin dosyası (veya AGENT_CURL env)"
    )
    p_ins.add_argument(
        "--max-curl-file",
        help="İsteğe bağlı Max cURL dosyası (hesap ayrılığı kontrolü için)",
    )
    p_ins.add_argument("--chat-id", help="cURL'de yoksa Agent sohbet UUID'si")

    # check-agent
    p_chk = sub.add_parser(
        "check-agent", help="Agent sohbetinin canlı durumunu kontrol eder."
    )
    p_chk.add_argument(
        "--agent-curl-file", help="Agent Mode cURL metin dosyası (veya AGENT_CURL env)"
    )
    p_chk.add_argument("--chat-id", help="cURL'de yoksa Agent sohbet UUID'si")

    # ortak run / resume seçenekleri
    def _add_common_run_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--agent-curl-file",
            help="Agent Mode '/input' cURL dosyası (veya AGENT_CURL env)",
        )
        target.add_argument("--chat-id", help="cURL'de yoksa Agent sohbet UUID'si")
        target.add_argument(
            "--max-url",
            help="Canlı/yerel ApiWrapper kök URL'si (ör. https://...onrender.com)",
        )
        target.add_argument("--max-api-key", help="ApiWrapper API_KEYS anahtarı")
        target.add_argument(
            "--max-endpoint",
            choices=("chat", "ask"),
            default="chat",
            help="Dış Max sunucusunda kullanılacak uç ('ask' 30sn nabızla proxy kesmesini önler)",
        )
        target.add_argument(
            "--max-model", default="max", help="Max model adı (varsayılan: max)"
        )
        target.add_argument(
            "--max-curl-file",
            help="Sunucusuz kullanım için ayrı Google hesabından Max cURL dosyası",
        )
        target.add_argument(
            "--max-turns", type=int, default=12, help="Üst tur sınırı (varsayılan: 12)"
        )
        target.add_argument(
            "--auto-turns",
            action="store_true",
            default=False,
            help="Max projenin kapsamına göre ideal tur sayısını otomatik belirlesin",
        )
        target.add_argument(
            "--night-window",
            action="store_true",
            default=False,
            help="Yalnız 22:00–07:00 (TR) gece penceresinde çalıştır",
        )
        target.add_argument(
            "--gap-min",
            type=float,
            default=15.0,
            help="İstekler arası asgari bekleme (sn, varsayılan: 15)",
        )
        target.add_argument(
            "--gap-max",
            type=float,
            default=30.0,
            help="İstekler arası azami bekleme (sn, varsayılan: 30)",
        )
        target.add_argument(
            "--agent-timeout",
            type=float,
            default=1500.0,
            help="Tur başına azami Agent bekleme süresi (sn, varsayılan: 1500)",
        )
        target.add_argument(
            "--state-file",
            default="autopilot_state.json",
            help="Gizli değer içermeyen ilerleme/hafıza kayıt dosyası",
        )
        target.add_argument(
            "--no-stream-print",
            dest="stream_agent",
            action="store_false",
            default=True,
            help="Agent'ın canlı metin/araç akışını terminale yazdırma",
        )

    p_run = sub.add_parser("run", help="Yeni bir kendi kendine yürüyen proje başlatır.")
    p_run.add_argument("--idea", help="Proje fikri metni")
    p_run.add_argument("--idea-file", help="Proje fikrini içeren metin dosyası")
    _add_common_run_flags(p_run)

    p_res = sub.add_parser("resume", help="Kaydedilmiş durum dosyasından projeye devam eder.")
    _add_common_run_flags(p_res)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows PowerShell (cp1254/cp857) üzerinde Türkçe karakter ve simgelerin
    # UnicodeEncodeError fırlatmaması için akışları UTF-8'e sabitle.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return _cmd_inspect(args)
    try:
        if args.command == "check-agent":
            return asyncio.run(_cmd_check_agent_async(args))
        if args.command == "run":
            return asyncio.run(_run_or_resume_async(args, resume=False))
        if args.command == "resume":
            return asyncio.run(_run_or_resume_async(args, resume=True))
    except KeyboardInterrupt:
        print("\n⏹️ Kullanıcı tarafından durduruldu (Ctrl+C).", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
