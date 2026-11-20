"""The desktop program's pieces agree with each other: ``desktop.json``, the Tauri shell (Rust constants, window and product names, versions, installer
resources), the bundle spec and the application's own protocol line and options. Nothing here needs Rust, Tauri or Windows."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import app as cadapp
from utils import desktop_support as support

APP = Path(__file__).resolve().parent.parent
TAURI = APP / "src-tauri"
DESC = json.loads((APP / "desktop.json").read_text(encoding="utf-8"))
RUST = (TAURI / "src" / "desktop.rs").read_text(encoding="utf-8")


def rust_const(name: str) -> str:
    m = re.search(rf'const {name}: (?:&str|u16) = "?([^";]+)"?;', RUST)
    assert m, name
    return m.group(1)


def test_description_names_this_application():
    assert DESC["slug"] == support.SLUG == "cadcon"
    assert DESC["title"] == support.TITLE
    assert DESC["productName"] == support.PRODUCT_NAME
    assert DESC["identifier"] == support.IDENTIFIER
    assert DESC["port"] == support.DESKTOP_PORT
    assert DESC["module"] == "applications.CADCon"
    assert (APP / "desktop" / "requirements-standard.txt").is_file()


def test_the_window_port_is_not_the_browser_versions():
    config = (APP / "configs" / "cadcon_config.yaml").read_text(encoding="utf-8")
    browser_port = int(re.search(r"^\s*port:\s*(\d+)", config, re.M).group(1))
    assert DESC["port"] != browser_port               # both can run at once


def test_the_rust_shell_speaks_the_same_protocol():
    assert rust_const("READY_PREFIX") == support.prefix() + "_READY "
    assert rust_const("ERROR_PREFIX") == support.prefix() + "_ERROR "
    assert rust_const("TITLE") == support.TITLE
    assert rust_const("MODULE") == DESC["module"]
    assert int(rust_const("PREFERRED_PORT")) == DESC["port"]


def test_the_rust_shell_starts_the_bundled_python_with_the_desktop_options():
    for flag in ("--no-browser", "--ready-line", "--exit-on-stdin-close", "--port-fallback", '"-u"', '"utf8"'):
        assert flag in RUST, flag
    assert '"runtime"' in RUST and '"slai-root"' in RUST and "python.exe" in RUST
    assert support.BUNDLE_SLAI_DIR == "slai-root"
    assert "window.cadconFailed" in RUST
    assert "window.cadconFailed" in (TAURI / "splash" / "splash.js").read_text(encoding="utf-8")


def test_every_flag_the_shell_passes_is_one_the_application_accepts():
    parser = cadapp._parser()
    known = {s for action in parser._actions for s in action.option_strings}
    for flag in ("--no-browser", "--ready-line", "--exit-on-stdin-close", "--port-fallback", "--port", "--client-dir", "--check", "--json",
                 "--require-agents", "--verify-files"):
        assert flag in known, flag


def test_names_and_versions_agree():
    conf = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["productName"] == DESC["productName"]
    assert conf["identifier"] == DESC["identifier"]
    assert conf["app"]["windows"][0]["title"] == DESC["productName"]
    assert conf["version"] == cadapp.VERSION
    assert f'version = "{cadapp.VERSION}"' in (TAURI / "Cargo.toml").read_text(encoding="utf-8")
    assert json.loads((APP / "package.json").read_text(encoding="utf-8"))["version"] == cadapp.VERSION
    for icon in conf["bundle"]["icon"]:
        assert (TAURI / icon).is_file(), icon
    assert (TAURI / conf["bundle"]["windows"]["nsis"]["installerHooks"]).is_file()
    assert conf["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"      # the install folder is written to while the program runs


def test_the_shell_grants_the_page_nothing_and_ships_the_engine_folder():
    cap = json.loads((TAURI / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert cap["permissions"] == []
    win = json.loads((TAURI / "tauri.windows.conf.json").read_text(encoding="utf-8"))
    assert win["bundle"]["resources"] == {"engine/": "engine/"}
    assert win["bundle"]["targets"] == ["nsis"]


def test_the_installer_replaces_the_engine_folder_cleanly():
    hooks = (TAURI / "installer" / "hooks.nsh").read_text(encoding="utf-8")
    assert 'RMDir /r "$INSTDIR\\engine"' in hooks
    assert "NSIS_HOOK_PREINSTALL" in hooks and "NSIS_HOOK_POSTUNINSTALL" in hooks


def test_the_bundle_spec_ships_what_the_shell_runs():
    spec = json.loads((APP / "desktop" / "bundle-spec.json").read_text(encoding="utf-8"))
    assert "applications.CADCon.app" in spec["slai"]["entry_modules"]
    assert "app.py" in spec["app"]["required"] and "dist/index.html" in spec["app"]["required"]
    assert not any(item.startswith("src-tauri") for item in spec["app"]["include"]), "the installer must not contain its own build folder"
    assert spec["runtime"]["python_series"] == "3.13"
    assert set(spec["runtime"]["profiles"]) == {"standard", "full"}
    for rel in {r for rows in spec["runtime"]["profiles"].values() for r in rows} | {spec["runtime"]["optional"]}:
        assert (APP / rel).is_file(), rel


def test_the_start_up_screen_is_the_shared_one():
    splash = (TAURI / "splash" / "index.html").read_text(encoding="utf-8")
    assert 'href="startup/loader.css"' in splash
    assert 'class="slai-startup spadra-startup slai-startup-live"' in splash
    assert f"<h1>{support.TITLE}</h1>" in splash
    assert (TAURI / "splash" / "logo.png").stat().st_size > 0
    assert "npm run splash" in json.loads((APP / "package.json").read_text(encoding="utf-8"))["scripts"]["app:build:windows"]
    assert not re.search(r"<script>[^<]", splash) and "style=" not in splash         # the CSP allows no inline code


def test_the_csp_allows_only_the_local_splash():
    csp = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))["app"]["security"]["csp"]
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp and "default-src 'self'" in csp


def test_build_outputs_are_ignored_by_git():
    ignore = (APP / ".gitignore").read_text(encoding="utf-8")
    for entry in ("src-tauri/engine/", "src-tauri/target/", "src-tauri/splash/startup/"):
        assert entry in ignore


def test_a_failed_start_is_one_error_line_for_the_window(tmp_path):
    empty, data = tmp_path / "empty", tmp_path / "data"
    empty.mkdir()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cadapp.main(["--no-browser", "--ready-line", "--client-dir", str(empty), "--data-dir", str(data)])
        except SystemExit as exit_:
            code = exit_.code
    assert code not in (0, None)
    lines = [line for line in out.getvalue().splitlines() if line.startswith(support.prefix() + "_ERROR ")]
    assert len(lines) == 1, out.getvalue()


def test_without_the_ready_option_no_protocol_line_is_printed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cadapp.main(["--no-browser", "--client-dir", str(empty), "--data-dir", str(tmp_path / "data")])
        except SystemExit:
            pass
    assert "_ERROR " not in out.getvalue()
