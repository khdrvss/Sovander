"""Packaging (task 6.1): desktop entry, AppArmor profile, launcher, .deb and offline bundle.

Requirements 1.5 (Open With, no default changed), 9.1 and 9.7 (no internet for the app and its
children), 13 (install, offline install, nothing left behind) and the NFR Size budget.
Tests that need dpkg-deb, apparmor_parser or apt skip where those are missing.
"""

from __future__ import annotations

import configparser
import gettext
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import sovander

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "data" / "io.github.khdrvss.Sovander.desktop"
PROFILE = ROOT / "packaging" / "apparmor" / "sovander"
LAUNCHER = ROOT / "packaging" / "sovander.sh"
BUILD_DEB = ROOT / "packaging" / "build-deb.sh"
BUNDLE = ROOT / "packaging" / "make-offline-bundle.sh"
MAINTAINER_SCRIPTS = [ROOT / "packaging" / "debian" / s for s in ("postinst", "prerm", "postrm")]
SIZE_BUDGET = 20 * 1024 * 1024  # NFR Size: the Viewer's own installed files

# One MIME type per format named in Requirement 2 (the canonical shared-mime-info name)
REQUIRED_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "doc": "application/msword",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "xls": "application/vnd.ms-excel",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "odg": "application/vnd.oasis.opendocument.graphics",
    "epub": "application/epub+zip",
    "markdown": "text/markdown",
    "text": "text/plain",
    "log": "text/x-log",
    "python": "text/x-python3",
    "c": "text/x-csrc",
    "json": "application/json",
    "xml": "application/xml",
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "tiff": "image/tiff",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
    "zip": "application/zip",
}

DEPENDS = [
    "python3 (>= 3.10)",
    "python3-gi",
    "gir1.2-gtk-4.0",
    "gir1.2-adw-1",
    "gir1.2-gtksource-5",
    "gir1.2-webkit-6.0",
    "python3-fitz",
    "python3-uno",
    "libreoffice-writer-nogui | libreoffice-writer",
    "libreoffice-calc-nogui | libreoffice-calc",
    "libreoffice-impress-nogui | libreoffice-impress",
    "libreoffice-draw-nogui | libreoffice-draw",
    "python3-cryptography",
    "python3-charset-normalizer",
    "fonts-crosextra-carlito",
    "fonts-crosextra-caladea",
    "fonts-liberation2",
    "apparmor",
    "heif-gdk-pixbuf",
    "libavif-gdk-pixbuf",
]

needs_dpkg_deb = pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb not installed")
needs_parser = pytest.mark.skipif(
    shutil.which("apparmor_parser") is None, reason="apparmor_parser not installed"
)


def run(*args, check=True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=300, **kwargs)


# Desktop entry (1.5)


def desktop_entry() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # keys are case-sensitive: Name[ru], MimeType
    parser.read(DESKTOP, encoding="utf-8")
    return parser["Desktop Entry"]


def test_desktop_entry_parses_and_names_the_app_in_both_languages():
    entry = desktop_entry()
    assert entry["Type"] == "Application"
    assert entry["Name"] == "Sovander"
    for key in ("Name[ru]", "Comment", "Comment[ru]", "GenericName[ru]"):
        assert entry[key].strip(), key
    assert re.search("[а-яё]", entry["Comment[ru]"], re.IGNORECASE)
    assert entry["Exec"].split() == ["sovander", "%U"]
    assert entry["Terminal"] == "false"


def test_desktop_entry_lists_every_format_of_requirement_2():
    listed = [t for t in desktop_entry()["MimeType"].split(";") if t]
    missing = {fmt: mime for fmt, mime in REQUIRED_TYPES.items() if mime not in listed}
    assert not missing
    assert len(listed) == len(set(listed)), "duplicate MIME types"
    assert desktop_entry()["MimeType"].endswith(";")


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils missing")
def test_desktop_entry_is_valid():
    result = run("desktop-file-validate", str(DESKTOP), check=False)
    assert result.returncode == 0 and not result.stdout.strip(), result.stdout + result.stderr


def test_installing_never_sets_default_applications():
    """An app that lists types joins "Open With"; only mimeapps.list or xdg-mime would change defaults."""
    owned = [DESKTOP, *(ROOT / "packaging").rglob("*")]
    for path in owned:
        assert path.name != "mimeapps.list"
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "xdg-mime" not in text and "mimeapps.list" not in text, path


# AppArmor profile (9.1, 9.7)


def profile_rules(text: str) -> list[str]:
    """Rule lines without comments; "#abi4# " lines count as rules, as they are in the 4.0 variant."""
    rules = []
    for line in text.splitlines():
        line = re.sub(r"^\s*#abi4#\s*", "", line)
        line = line.split("#", 1)[0].strip()
        if line:
            rules.append(line)
    return rules


def test_profile_denies_internet_in_every_profile_block():
    text = PROFILE.read_text(encoding="utf-8")
    blocks = re.split(r"^\s*profile\s+", text, flags=re.MULTILINE)[1:]
    assert len(blocks) == 2, "the sovander profile and its bwrap child"
    for block in blocks:
        name = block.split()[0]
        rules = profile_rules(block)
        assert "deny network inet," in rules, name
        assert "deny network inet6," in rules, name


def test_profile_allows_no_internet_socket():
    for rule in profile_rules(PROFILE.read_text(encoding="utf-8")):
        if rule.startswith("deny "):
            continue
        rule = re.sub(r"^((allow|audit)\s+)+", "", rule)
        assert not re.match(r"^network\s*(inet6?\b|,|\s+(stream|dgram|raw|tcp|udp)\b)", rule), rule
        assert not re.match(r"^all\s*,", rule), rule


def exec_permissions(rule: str) -> str | None:
    """The permission letters of a path rule that may execute, e.g. "ix" or "Cx"; None otherwise."""
    if rule.startswith("deny "):
        return None
    words = [
        w for w in rule.rstrip(",").split("->")[0].split() if w not in ("allow", "audit", "owner", "file")
    ]
    if len(words) != 2:
        return None
    path, perms = words if words[0].startswith(("/", "@")) else words[::-1]
    if not path.startswith(("/", "@")):
        return None
    return perms if "x" in perms.lower() else None


def test_every_child_program_stays_confined():
    """ix keeps a child in this profile, Cx in its child profile; px, ux and pix would leave it."""
    seen = []
    for rule in profile_rules(PROFILE.read_text(encoding="utf-8")):
        perms = exec_permissions(rule)
        if perms is not None:
            seen.append(perms)
            assert re.fullmatch(r"[rwlkm]*ix[rwlkm]*|Cx", perms), rule
    assert "Cx" in seen and "ix" in seen


def test_profile_has_no_userns_rule_outside_the_abi4_marker():
    """AppArmor 3.0 (Ubuntu 22.04) rejects userns rules, so they must stay behind "#abi4# "."""
    text = PROFILE.read_text(encoding="utf-8")
    assert re.search(r"^\s*abi <abi/3\.0>,", text, re.MULTILINE)
    for line in text.splitlines():
        if re.search(r"\buserns\b", line.split("#", 1)[0]):
            pytest.fail(f"userns outside a #abi4# line: {line!r}")
    assert re.search(r"^\s*#abi4#\s*userns,", text, re.MULTILINE)


@needs_parser
def test_profile_compiles_for_the_installed_apparmor():
    result = run("apparmor_parser", "-Q", "-K", "-T", str(PROFILE), check=False)
    assert result.returncode == 0, result.stderr


@needs_parser
@needs_dpkg_deb
@pytest.mark.skipif(not Path("/etc/apparmor.d/abi/4.0").exists(), reason="AppArmor 4 not installed")
def test_abi4_variant_compiles_with_userns(extracted):
    variant = extracted / "usr/share/sovander/apparmor/sovander.abi4"
    expected = re.sub(r"^abi <abi/3\.0>,", "abi <abi/4.0>,", PROFILE.read_text(encoding="utf-8"), flags=re.M)
    expected = re.sub(r"^(\s*)#abi4# ", r"\1", expected, flags=re.M)
    assert variant.read_text(encoding="utf-8") == expected
    assert re.search(r"^\s*userns,", expected, re.MULTILINE)
    result = run("apparmor_parser", "-Q", "-K", "-T", str(variant), check=False)
    assert result.returncode == 0, result.stderr


# Shell scripts


@pytest.mark.parametrize("script", sorted((ROOT / "packaging").glob("*.sh")), ids=lambda p: p.name)
def test_shell_scripts_parse_and_are_strict(script):
    assert run("bash", "-n", str(script), check=False).returncode == 0
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


@pytest.mark.parametrize("script", MAINTAINER_SCRIPTS, ids=lambda p: p.name)
def test_maintainer_scripts_parse(script):
    assert run("sh", "-n", str(script), check=False).returncode == 0
    assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\n")


def test_postinst_never_fails_the_install_over_apparmor():
    text = (ROOT / "packaging" / "debian" / "postinst").read_text(encoding="utf-8")
    load = next(line for line in text.splitlines() if "apparmor_parser -r" in line)
    assert load.rstrip().endswith("||"), "a failed load must fall through to a warning"


def fake_app(tmp_path: Path) -> Path:
    """A stand-in sovander package that reports how the launcher started it.

    The test runs from a folder that holds a decoy "sovander" package: python3 -m would import that
    one, because it puts the current folder first on the import path.
    """
    pkg = tmp_path / "lib" / "sovander"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        "import os, sys\n"
        "print(os.environ.get('SOVANDER_CONFINEMENT'))\n"
        "print(sys.path[0])\n"
        "print('|'.join(sys.argv[1:]))\n"
    )
    decoy = tmp_path / "cwd" / "sovander"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("")
    (decoy / "__main__.py").write_text("print('decoy')\n")
    return pkg.parent


def test_launcher_runs_unconfined_when_aa_exec_is_missing(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("tr", "bash"):
        (tools / tool).symlink_to(shutil.which(tool))
    lib = fake_app(tmp_path)
    env = {"PATH": str(tools), "SOVANDER_LIBDIR": str(lib), "PYTHONPATH": str(tmp_path / "cwd")}
    result = run(str(LAUNCHER), "a b.docx", "--flag", env=env, cwd=tmp_path / "cwd")
    confinement, path0, args = result.stdout.splitlines()
    assert confinement == "none"
    assert path0 == str(lib)
    assert args == "a b.docx|--flag"


@pytest.mark.skipif(shutil.which("aa-exec") is None, reason="aa-exec not installed")
def test_launcher_confines_exactly_when_the_profile_is_loaded(tmp_path):
    loaded = run("aa-exec", "-p", "sovander", "--", "/bin/true", check=False).returncode == 0
    env = {**os.environ, "SOVANDER_LIBDIR": str(fake_app(tmp_path))}
    result = run(str(LAUNCHER), "x.pdf", env=env, cwd=tmp_path / "cwd")
    assert result.stdout.splitlines()[0] == ("apparmor" if loaded else "none")


# The .deb (13.1, 13.4, NFR Size)


@pytest.fixture(scope="session")
def deb(tmp_path_factory) -> Path:
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb not installed")
    out = tmp_path_factory.mktemp("dist")
    path = Path(run(str(BUILD_DEB), str(out)).stdout.strip())
    assert path.is_file() and path.parent == out
    return path


@pytest.fixture(scope="session")
def extracted(deb, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("root")
    run("dpkg-deb", "-x", str(deb), str(root))
    return root


def deb_field(deb: Path, name: str) -> str:
    return run("dpkg-deb", "-f", str(deb), name).stdout.strip()


@needs_dpkg_deb
def test_deb_control_fields(deb):
    assert deb_field(deb, "Package") == "sovander"
    assert deb_field(deb, "Version") == sovander.__version__
    assert deb_field(deb, "Architecture") == "all"
    depends = [d.strip() for d in deb_field(deb, "Depends").split(",")]
    assert depends == DEPENDS
    info = run("dpkg-deb", "-I", str(deb)).stdout
    for script in ("postinst", "prerm", "postrm"):
        assert re.search(rf"\s\*\s+{script}\s", info), script


@needs_dpkg_deb
def test_deb_contents(deb):
    listing = run("dpkg-deb", "-c", str(deb)).stdout.splitlines()
    entries = {line.split(None, 5)[5].split(" -> ")[0]: line for line in listing}
    for path in (
        "./usr/bin/sovander",
        "./usr/share/applications/io.github.khdrvss.Sovander.desktop",
        "./usr/share/sovander/apparmor/sovander",
        "./usr/share/sovander/apparmor/sovander.abi4",
        "./usr/lib/sovander/sovander/__init__.py",
        "./usr/share/sovander/engine-profile.xcu",
        "./usr/share/locale/ru/LC_MESSAGES/sovander.mo",
        "./usr/share/metainfo/io.github.khdrvss.Sovander.metainfo.xml",
        "./usr/share/icons/hicolor/scalable/apps/io.github.khdrvss.Sovander.svg",
        "./usr/share/doc/sovander/copyright",
    ):
        assert path in entries, path
    assert entries["./usr/bin/sovander"].startswith("-rwxr-xr-x root/root")
    assert entries["./usr/lib/sovander/data"].endswith("-> ../../share/sovander")
    for line in listing:
        assert " root/root " in line, line
        assert "__pycache__" not in line and ".pyc" not in line, line
        assert "mimeapps.list" not in line, line


@needs_dpkg_deb
def test_deb_installs_a_working_layout(extracted):
    assert (extracted / "usr/bin/sovander").read_bytes() == LAUNCHER.read_bytes()
    assert (extracted / "usr/lib/sovander/data/engine-profile.xcu").is_file()  # the app's own lookup
    assert (extracted / "usr/share/sovander/apparmor/sovander").read_bytes() == PROFILE.read_bytes()
    with open(extracted / "usr/share/locale/ru/LC_MESSAGES/sovander.mo", "rb") as f:
        catalog = gettext.GNUTranslations(f)
    assert catalog.gettext("Offline: verified by the system") == "Офлайн: подтверждено системой"


@needs_dpkg_deb
def test_own_installed_files_fit_the_size_budget(deb):
    assert int(deb_field(deb, "Installed-Size")) * 1024 <= SIZE_BUDGET
    listing = run("dpkg-deb", "-c", str(deb)).stdout.splitlines()
    assert sum(int(line.split()[2]) for line in listing) <= SIZE_BUDGET


# Offline bundle (13.3)


def apt_resolves() -> bool:
    return shutil.which("apt-get") is not None and Path("/var/lib/apt/lists").is_dir()


@needs_dpkg_deb
@pytest.mark.skipif(not apt_resolves(), reason="apt not available")
def test_offline_bundle_dry_run_lists_the_dependency_closure(deb):
    result = run(str(BUNDLE), "--dry-run", str(deb), check=False)
    if result.returncode != 0 and "no dependencies" in result.stderr:
        pytest.skip("apt package lists are empty")
    assert result.returncode == 0, result.stderr
    packages = result.stdout.splitlines()
    names = {line.split(":", 1)[0] for line in packages}
    assert all(re.fullmatch(r"[a-z0-9.+-]+:(amd64|all)=\S+", line) for line in packages), packages[:5]
    assert "sovander" not in names
    for dep in ("python3-fitz", "python3-uno", "gir1.2-webkit-6.0", "libreoffice-core", "apparmor"):
        assert dep in names, dep
    assert re.search(r"\d+ packages, \d+ MB to download", result.stderr)


@needs_dpkg_deb
@pytest.mark.skipif(not apt_resolves(), reason="apt not available")
def test_offline_bundle_holds_every_package_and_the_instructions(deb, tmp_path):
    """The real download is hundreds of MB; a stand-in apt-get download writes empty files instead."""
    shim = tmp_path / "shim"
    shim.mkdir()
    real_apt = shutil.which("apt-get")
    (shim / "apt-get").write_text(
        "#!/bin/bash\n"
        'if [[ " $* " == *" download "* ]]; then\n'
        '  for spec in "$@"; do [[ $spec == *=* ]] || continue\n'
        '    name=${spec%%:*}; rest=${spec#*:}; touch "${name}_${rest#*=}_${rest%%=*}.deb"; done\n'
        "  exit 0\n"
        "fi\n"
        f'exec {real_apt} "$@"\n'
    )
    (shim / "apt-get").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    result = run(str(BUNDLE), "--out", str(tmp_path / "out"), str(deb), env=env, check=False)
    if result.returncode != 0 and "no dependencies" in result.stderr:
        pytest.skip("apt package lists are empty")
    assert result.returncode == 0, result.stderr
    tarball = Path(result.stdout.strip())
    assert re.fullmatch(rf"sovander-offline-{re.escape(sovander.__version__)}-[a-z]+\.tar", tarball.name)
    with tarfile.open(tarball) as tar:
        names = tar.getnames()
        top = tarball.name.removesuffix(".tar")
        assert all(n == top or n.startswith(top + "/") for n in names)
        install = tar.extractfile(f"{top}/INSTALL.txt").read().decode("utf-8")
    assert f"{top}/{deb.name}" in names
    assert sum(n.endswith(".deb") for n in names) > 100
    assert "sudo apt install --no-install-recommends ./*.deb" in install
