"""Paket sürümleri ve taban imaj SABİT mi? (operatör kararı, 19 Kasım 2026)

Kural: bir imaj derlemesi, kod değişmeden upstream'e gidenleri değiştirememeli.
Kilitten önce `pip install ".[tokens,browser]"` her derlemede `pyproject.toml`
aralığındaki en yeni sürümleri kuruyor, `FROM python:3.12-slim` de etiketin o gün
gösterdiği imajı çekiyordu: 17–19 Kasım arasında yalnız iki günde fastapi üç,
pydantic bir sürüm çıkardı. Gönderen yığın (httpx, httpcore, h2 …) o günlerde
değişmedi, ama bunu sağlayan şans değil kilit olmalı.

Bu dosya üç şeyi kilitler:

1. `requirements.lock` tam sürümler içerir, `pyproject.toml` aralığının içindedir
   ve upstream'e konuşan yığını canlı derlemenin kurduğu sürümlerde tutar.
2. İki `Dockerfile` da aynı taban imaj özetinden başlar, paketleri kilitle kurar
   ve derlemenin sonunda `scripts/verify_lock.py` ile imajın içini doğrular.
3. CI kilidi Python 3.12'de kurar ve imajı gerçekten derleyip testleri içinde koşar
   (Dockerfile'ın derlendiğinin tek kanıtı odur).

Kilidi ya da taban imajı güncellemek gönderme tarafını değiştirebilir; bilerek
yapılır. Adımlar: `docs/LESSONS_2026_11_14.md` §20.6.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

from scripts.verify_lock import (
    IGNORED,
    WIRE_STACK,
    compare,
    compare_runtime,
    installed_versions,
    main,
    openssl_version,
    parse_lock,
    summary_line,
)

ROOT = Path(__file__).resolve().parents[1]
PINS = parse_lock((ROOT / "requirements.lock").read_text(encoding="utf-8"))
IMAGES = ("Dockerfile", "Dockerfile.telegram")

#: Upstream'e konuşan yığın ve canlı derlemenin (03aca2c, 17 Kasım 2026) kurduğu
#: sürümler. Hepsi o tarihten önce yayımlandı ve 19 Kasım'a kadar yenisi çıkmadı
#: (PyPI kayıtları), yani o gün kurulan ile bugün kurulacak olan aynıdır. Bu
#: sözlüğü değiştiren, upstream'e giden isteği taşıyan kütüphaneyi değiştiriyordur.
LIVE_WIRE_STACK = {
    "httpx": "0.28.1",
    "httpcore": "1.0.9",
    "h2": "4.4.1",
    "hpack": "4.2.0",
    "hyperframe": "6.1.0",
    "h11": "0.16.0",
    "anyio": "4.16.0",
    "certifi": "2026.10.23",
    "idna": "3.20",
}


# ------------------------------------------------------------------ yardımcılar
def instructions(name: str) -> list[str]:
    """Dockerfile'ın talimatları: yorumlar atılmış, devam satırları birleştirilmiş."""
    lines = [
        line
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    joined = "\n".join(lines).replace("\\\n", " ")
    return [re.sub(r"\s+", " ", line).strip() for line in joined.splitlines() if line.strip()]


def shell_commands(name: str) -> list[str]:
    """`RUN` talimatlarındaki komutlar, Dockerfile'daki sırasıyla."""
    return [
        command.strip()
        for instruction in instructions(name)
        if instruction.startswith("RUN ")
        for command in re.split(r"&&|\|\||;", instruction[4:])
    ]


def copy_sources(name: str) -> list[str]:
    """Derleme bağlamından imaja kopyalanan yollar."""
    sources: list[str] = []
    for instruction in instructions(name):
        if instruction.startswith("COPY "):
            parts = [part for part in instruction.split()[1:] if not part.startswith("--")]
            sources += parts[:-1]
    return sources


def in_build_context(path: str) -> bool:
    """`.dockerignore`: yol (ya da üst dizini) için SON eşleşen kural kazanır."""
    pieces = path.split("/")
    candidates = ["/".join(pieces[: index + 1]) for index in range(len(pieces))]
    excluded = False
    for raw in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        pattern = rule.lstrip("!").strip("/")
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            excluded = not rule.startswith("!")
    return not excluded


def verify_arguments(name: str) -> list[str]:
    commands = [command for command in shell_commands(name) if "verify_lock.py" in command]
    assert len(commands) == 1, f"{name}: tam bir `verify_lock.py` çağrısı bekleniyor"
    return shlex.split(commands[0])


def option(arguments: list[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


# ----------------------------------------------------------------------- kilit
def test_lock_keeps_the_wire_stack_on_the_versions_the_live_build_installed():
    assert {name: PINS.get(name) for name in LIVE_WIRE_STACK} == LIVE_WIRE_STACK
    # Derleme loguna yazılan yığın da aynı kümedir.
    assert set(WIRE_STACK) == set(LIVE_WIRE_STACK)


def test_every_lock_entry_is_one_exact_released_version():
    for name, version in PINS.items():
        parsed = Version(version)
        assert not parsed.is_prerelease, f"{name}=={version}: ön sürüm kilitlenmez"
        assert parsed.local is None, f"{name}=={version}: yerel sürüm kilitlenmez"


def test_lock_covers_every_production_dependency_inside_the_declared_range():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+; içerik yorumlayıcıdan bağımsız
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    production = [*project["dependencies"], *extras["tokens"], *extras["browser"]]

    assert len(production) >= 10
    for line in production:
        requirement = Requirement(line)
        name = requirement.name.lower()
        assert name in PINS, f"{name} kilitte yok"
        assert requirement.specifier.contains(PINS[name]), (
            f"{name}=={PINS[name]} pyproject.toml aralığının dışında: {requirement.specifier}"
        )

    # Kilit yalnız üretim imajını tarif eder; test araçları ona girmez.
    for line in extras["dev"]:
        assert Requirement(line).name.lower() not in PINS


def test_lock_parser_accepts_only_exact_pins():
    text = "# yorum\n\nPyYAML==6.0.3\ntyping_extensions==4.16.0  # satır içi yorum\n"
    assert parse_lock(text) == {"pyyaml": "6.0.3", "typing-extensions": "4.16.0"}

    for broken in (
        "httpx>=0.27\n",  # aralık
        "httpx\n",  # sürümsüz
        'httpx==0.28.1; python_version < "3.11"\n',  # koşullu
        "-r other.txt\n",  # başka dosyaya gönderme
        "httpx==0.28.1\nHTTPX==0.27.0\n",  # aynı paket iki kez
        "# yalnız yorum\n",  # boş kilit
    ):
        with pytest.raises(ValueError):
            parse_lock(broken)


# -------------------------------------------------------------------- denetçi
def test_verifier_reports_changed_missing_and_unlisted_packages():
    pins = {"httpx": "0.28.1", "h2": "4.4.1", "playwright": "1.65.2"}
    installed = {"httpx": "0.29.0", "h2": "4.4.1", "pytest": "8.4.2"}

    strict = compare(pins, installed)
    assert len(strict) == 3
    assert any("httpx: kurulu 0.29.0, kilit 0.28.1" in line for line in strict)
    assert any("KURULU DEĞİL" in line and "playwright==1.65.2" in line for line in strict)
    assert any("KİLİTTE YOK" in line and "pytest==8.4.2" in line for line in strict)

    # --subset: eksik paket sorun değil. --allow-extra: fazladan paket sorun değil.
    # Sürümü değişen paket her kipte sorundur.
    subset = compare(pins, installed, subset=True)
    assert len(subset) == 2 and not any("KURULU DEĞİL" in line for line in subset)
    extra = compare(pins, installed, allow_extra=True)
    assert len(extra) == 2 and not any("KİLİTTE YOK" in line for line in extra)
    both = compare(pins, installed, subset=True, allow_extra=True)
    assert len(both) == 1 and "SÜRÜM FARKLI" in both[0]

    assert compare(pins, dict(pins)) == []


def test_verifier_checks_the_interpreter_and_its_openssl():
    assert compare_runtime(python="3.12.16", openssl="3.5.7") == []
    assert (
        compare_runtime(
            python="3.12.16", openssl="3.5.7", expect_python="3.12.16", expect_openssl="3.5.7"
        )
        == []
    )
    problems = compare_runtime(
        python="3.12.17", openssl="3.5.8", expect_python="3.12.16", expect_openssl="3.5.7"
    )
    assert problems == [
        "PYTHON FARKLI   çalışan 3.12.17, beklenen 3.12.16",
        "OPENSSL FARKLI  çalışan 3.5.8, beklenen 3.5.7",
    ]
    assert re.fullmatch(r"\d+\.\d+\.\d+", openssl_version())


class _Distribution:
    def __init__(self, name: str | None, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


def test_verifier_ignores_the_installer_and_the_app_itself(monkeypatch):
    found = [
        _Distribution("pip", "25.0.1"),  # taban imajla gelir
        _Distribution("setuptools", "80.9.0"),
        _Distribution("wheel", "0.45.1"),
        _Distribution("apiwrapper", "1.0.0"),  # uygulamanın kendisi
        _Distribution("PyYAML", "6.0.3"),  # adlar PEP 503 biçimine çevrilir
        _Distribution("typing_extensions", "4.16.0"),  # sys.path'te önce gelen sayılır
        _Distribution("typing-extensions", "4.0.0"),
        _Distribution(None, "1.0"),  # bozuk kayıt
    ]
    monkeypatch.setattr("scripts.verify_lock.metadata.distributions", lambda: iter(found))
    assert installed_versions() == {"pyyaml": "6.0.3", "typing-extensions": "4.16.0"}


def test_verifier_sees_this_environment_and_summarises_the_wire_stack():
    installed = installed_versions()
    assert "httpx" in installed and "pytest" in installed
    assert not IGNORED & set(installed)

    line = summary_line(installed, python="3.12.16", openssl="3.5.7")
    assert line.startswith("GÖNDERME YIĞINI python=3.12.16 openssl=3.5.7 httpx=")
    assert all(f" {name}=" in line for name in LIVE_WIRE_STACK)


def test_verifier_exit_code_stops_the_build_on_any_difference(tmp_path, capsys):
    installed = installed_versions()
    good = tmp_path / "good.lock"
    good.write_text("".join(f"{name}=={version}\n" for name, version in installed.items()))
    assert main([str(good)]) == 0
    assert "kilit uyumlu" in capsys.readouterr().out

    # Tek bir paketin sürümü farklı: derleme durur ve hangi paket olduğu yazılır.
    bad = tmp_path / "bad.lock"
    bad.write_text(good.read_text().replace(f"httpx=={installed['httpx']}", "httpx==0.0.1"))
    assert main([str(bad)]) == 1
    output = capsys.readouterr().out
    assert f"httpx: kurulu {installed['httpx']}, kilit 0.0.1" in output
    assert "LESSONS_2026_11_14.md §20.6" in output

    # Paketler aynı ama yorumlayıcı beklenen değil: yine durur.
    assert main([str(good), "--expect-python", "0.0.0"]) == 1
    assert "PYTHON FARKLI" in capsys.readouterr().out
    assert main([str(good), "--expect-openssl", "0.0.0"]) == 1
    assert "OPENSSL FARKLI" in capsys.readouterr().out


# -------------------------------------------------------------------- imajlar
def test_both_images_start_from_the_same_pinned_base_image_digest():
    digests = {}
    for name in IMAGES:
        froms = [line for line in instructions(name) if line.startswith("FROM ")]
        assert len(froms) == 1, f"{name}: tek aşamalı imaj bekleniyor"
        match = re.fullmatch(r"FROM python:3\.12-slim@(sha256:[0-9a-f]{64})( AS \w+)?", froms[0])
        assert match, f"{name}: taban imaj özetle (@sha256:…) sabitlenmemiş: {froms[0]}"
        digests[name] = match.group(1)
    assert len(set(digests.values())) == 1, digests


@pytest.mark.parametrize("name", IMAGES)
def test_image_installs_dependencies_only_through_the_lock(name: str):
    commands = shell_commands(name)
    installs = [command for command in commands if re.search(r"\bpip install\b", command)]
    assert installs, f"{name}: pip install bulunamadı"
    assert any("-c requirements.lock" in command for command in installs)
    for command in installs:
        # Bağımlılık çözebilen her kurulum kilitle kısıtlıdır; tek istisna hiçbir
        # bağımlılık kuramayan `--no-deps`.
        assert "-c requirements.lock" in command or "--no-deps" in command, command

    # Kilit, onu kullanan ilk kurulumdan önce imaja kopyalanmıştır.
    flat = instructions(name)
    copied = next(i for i, line in enumerate(flat) if line.startswith("COPY ") and "lock" in line)
    first_install = next(i for i, line in enumerate(flat) if "pip install" in line)
    assert copied < first_install


@pytest.mark.parametrize("name", IMAGES)
def test_image_build_ends_by_verifying_its_own_contents(name: str):
    arguments = verify_arguments(name)
    assert arguments[:3] == ["python", "scripts/verify_lock.py", "requirements.lock"]
    # Ek pakete hiçbir imajda izin yok; Telegram imajı kilidin yalnız bir kısmını kurar.
    assert "--allow-extra" not in arguments
    assert ("--subset" in arguments) == (name == "Dockerfile.telegram")
    # Taban imaj `python:3.12` olduğuna göre beklenen yorumlayıcı da 3.12'dir.
    assert re.fullmatch(r"3\.12\.\d+", option(arguments, "--expect-python"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", option(arguments, "--expect-openssl"))

    # Denetim, bağımlılıkları değiştirebilecek her kurulumdan SONRA çalışır.
    commands = shell_commands(name)
    verify_at = next(i for i, command in enumerate(commands) if "verify_lock.py" in command)
    last_install = max(i for i, command in enumerate(commands) if "pip install" in command)
    assert verify_at > last_install


def test_both_images_expect_the_same_interpreter_and_openssl():
    expectations = {
        name: (
            option(verify_arguments(name), "--expect-python"),
            option(verify_arguments(name), "--expect-openssl"),
        )
        for name in IMAGES
    }
    assert len(set(expectations.values())) == 1, expectations


@pytest.mark.parametrize("name", IMAGES)
def test_everything_the_image_copies_reaches_the_build_context(name: str):
    sources = copy_sources(name)
    assert "requirements.lock" in sources and "scripts/verify_lock.py" in sources
    for source in sources:
        assert (ROOT / source).exists(), f"{name}: {source} depoda yok"
        assert in_build_context(source), f"{name}: {source} .dockerignore ile dışlanıyor"

    # İstisna yalnız denetçi içindir; öteki betikler ve testler imaja girmez.
    assert not in_build_context("scripts/ask_client.py")
    assert not in_build_context("tests/test_api.py")


@pytest.mark.parametrize("name", IMAGES)
def test_image_takes_no_build_arguments(name: str):
    """Render ortam değişkenlerini derlemeye `--build-arg` olarak verir.

    Dockerfile bir `ARG` okusaydı env değişikliği imajın içeriğini değiştirebilir,
    gizli değerler de imaj katmanlarına yazılabilirdi.
    """
    assert not [line for line in instructions(name) if line.startswith("ARG ")]


# -------------------------------------------------------------------------- CI
def test_ci_installs_the_lock_on_the_production_python_and_builds_the_images():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    # Üretim yorumlayıcısında (3.12) paketler kilitle kurulur ve doğrulanır.
    assert "3.12" in jobs["test"]["strategy"]["matrix"]["python-version"]
    locked = [
        step
        for step in jobs["test"]["steps"]
        if "-c requirements.lock" in step.get("run", "")
    ]
    assert len(locked) == 1
    assert locked[0]["if"] == "matrix.python-version == '3.12'"
    assert "scripts/verify_lock.py requirements.lock" in locked[0]["run"]

    # İmaj gerçekten derlenir ve testler onun içinde koşar.
    runs = "\n".join(step.get("run", "") for step in jobs["image"]["steps"])
    assert re.search(r"docker build .*-t apiwrapper:ci \.", runs)
    assert "docker build --progress=plain -f Dockerfile.telegram" in runs
    assert "sh /work/scripts/ci_image_tests.sh" in runs
    script = (ROOT / "scripts/ci_image_tests.sh").read_text(encoding="utf-8")
    assert "-c /app/requirements.lock" in script
    assert "python -m pytest" in script
