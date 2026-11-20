# CADCon as a desktop app (Tauri)

CADCon ships as a Windows installer (**SLAI CADCon**, NSIS, per-user) built with Tauri 2, the same way SciCal and Shadow Sovereignty are. Unlike those two, which run in the SLAI checkout's Python environment, this installer is **self-contained**: it carries SLAI (the parts CADCon uses, including the agents and their data), CADCon, and a Python runtime with every package. Nothing has to exist on the computer beforehand except WebView2, which the installer downloads if it is missing.

> **Status: written, not built.** Everything here was written and tested without Windows, without network access for packages, and without Tauri's crates. What was verified and what was not is listed in [Verification status](#verification-status). Please build it on Windows before relying on it.

## What is in the installer

```
<install folder>\                       per-user and writable (a current-user NSIS install; the exact folder was not confirmed on a build)
  (the Tauri shell executable)          window and engine launcher
  engine\
    runtime\                            embeddable CPython 3.13 + site-packages (numpy, scipy, torch (CPU), pgmpy, trimesh, shapely, ...)
    slai-root\                          a SLAI tree, laid out like a checkout so the same code runs unchanged
      src\                              SLAI's source: agents, their models and data files, utilities
      checkpointing\  logs\             imported by the agent factory and the logger
      applications\tools\               SLAI's shared server, storage and desktop helpers
      applications\assets\              shared theme/assets
      applications\CADCon\              CADCon: app.py, engine.py, repair.py, utils\, slai\ (agent bridge), configs\, dist\ (web client), doc\
    bundle-manifest.json                versions, what was installed, SHA-256 and size of every file
    THIRD_PARTY_NOTICES.txt             licences of the installed packages
```

### Agents and models

CADCon talks to three SLAI agents through `src.agents.agent_factory.AgentFactory`: **Spatial**, **Reasoning** and **Verification**. The bundle contains their code and every data file they read (`src/agents/*`: JSON/YAML knowledge and configuration). **None of the three loads external model weights**, so there is nothing else to download. The traced hard dependencies are in `desktop/requirements-standard.txt`; note that the Reasoning agent imports `torch` and `pgmpy` (the factory's "torch-free" comment is wrong), which is why a CPU build of PyTorch is part of the standard profile and the main reason the installer is large.

Not bundled, on purpose:

| Item | Why |
|---|---|
| SLAI's LSTP / SLAILM models (`model/`) | CADCon does not use them. |
| ODA File Converter (DWG) | Separate licence. Install it yourself and set `conversion.dwg_executable`; until then DWG shows as unavailable. |
| PyQt5, selenium, psycopg2, elasticsearch, kafka, boto3 | Not used by CADCon's agents; excluded even in the `full` profile. |

`ezdxf` (DXF, MIT) and `ifcopenshell` (IFC, LGPL-3.0) are **optional**: the build tries each on its own, records whether it was installed in `bundle-manifest.json`, and CADCon then lists the format as available or unavailable. They are not in SLAI's venv, so no version is pinned. IfcOpenShell stays as plain replaceable files in `engine\runtime`, as the LGPL asks. If its Windows wheel for Python 3.13 does not exist, IFC import is unavailable and the build says so; nothing stands in for it.

### Profiles

* `standard` (default): what CADCon and its three agents need.
* `full`: also what the rest of `src/` imports at module level on a desktop (torchvision, transformers, sentence-transformers, librosa, opencv, matplotlib, ...; see `desktop/requirements-full-extra.txt`). Much larger. Use it only if you want other SLAI agents available in the same runtime.

## Building the installer (Windows)

Needs Node.js, Python 3.12+ (only to run the build script, which uses the standard library), Rust with the Visual Studio C++ Build Tools, internet access, and CADCon checked out at `SLAI\applications\CADCon`.

```
cd SLAI\applications\CADCon
build-windows.cmd            :: standard profile
build-windows.cmd full       :: full profile
```

which runs, in order:

1. `npm install` and `npm run build` (the web client into `dist\`).
2. `npm run splash` (copies the shared start-up screen into `src-tauri\splash\startup`).
3. `python tools\desktop\build_bundle.py --slai-root ..\..` which
   * checks that SLAI and CADCon have everything listed in `desktop\bundle-spec.json`;
   * copies the SLAI folders and CADCon into `src-tauri\engine\slai-root` (the include/exclude rules are in the spec; test folders and training artefacts are left out);
   * reads every import statement from the entry modules (including lazy ones) and **fails if an exclude pattern removed a SLAI file that is imported** inside a folder the bundle ships;
   * downloads the embeddable CPython from python.org and installs the wheels into `engine\runtime\Lib\site-packages` with `pip install --target` and `--platform win_amd64 --python-version 3.13` (it resolves for the bundle's interpreter, installs nothing on the build machine; PyTorch comes from `download.pytorch.org/whl/cpu`);
   * prunes (`torch\include`, tests, `.pyi`), pre-compiles byte code, writes `THIRD_PARTY_NOTICES.txt` and `bundle-manifest.json`;
   * **starts CADCon from the finished bundle** (`runtime\python.exe -m applications.CADCon --check --require-agents --verify-files`) and refuses the bundle unless the three agents load and every file matches its hash. `--allow-missing-agents` relaxes only the agent requirement.
4. `npm run tauri -- build` which packages `engine\` as the installer's resource folder. The installer is written to `src-tauri\target\release\bundle\nsis`.

Useful options of the build script: `--plan` (list and size what would be copied, no download), `--constraints <file>` (pin transitive dependencies; generate it on the machine with SLAI's venv: `venv\Scripts\python -m pip freeze > desktop\constraints.txt`), `--skip-optional`, `--python-version 3.13.x`, `--python-embed <zip>` (use a local download), `--no-compile`, `--no-verify`.

Only direct dependencies are pinned in `desktop/requirements-*.txt` (taken from SLAI's venv: Python 3.13.14, torch 2.15.0+cpu). Without a constraints file pip chooses the transitive ones, so two builds on different days can differ; `bundle-manifest.json` records what was installed.

## How it runs

1. The shell starts `engine\runtime\python.exe -u -X utf8 -m applications.CADCon --no-browser --ready-line --exit-on-stdin-close --port-fallback --port 47872` with the working folder `engine\slai-root` and `SLAI_ROOT` set. The flags are flags and not environment variables because the embeddable Python ignores `PYTHON*` variables. The working folder is SLAI's because its logger writes `logs\...` and some agents use paths relative to it, so the install folder must be writable (hence the per-user install).
2. The engine prints one line, `CADCON_READY {"url": ...}`, when it listens (or `CADCON_ERROR <message>` if it cannot start). The window shows the shared start-up screen meanwhile, then navigates to the engine's loopback URL. If the engine's output ends without either line (for example an import error) the window says so at once instead of waiting; the wait is limited to 4 minutes.
3. Port 47872 is preferred so that the window keeps its address (and browser storage); if it is taken, any free port is used. It differs from the browser version's 8770, so both can run together. A second launch of the app opens a second window and engine on another port (there is no single-instance lock).
4. When the window closes the shell closes the engine's stdin; the engine shuts down (and removes its session folder) and is killed after about 2 s if it has not. If the shell is killed, the engine notices that its window process is gone and exits by itself.
5. What the engine prints besides the protocol line goes to `%LOCALAPPDATA%\SLAI\CADCon\engine.log` (restarted when it passes 2 MB). Working files (sessions, reports) live in `%LOCALAPPDATA%\SLAI\CADCon` too. Both survive an update or uninstall.

The page the window shows never calls Tauri, so the shell grants it no permissions (`capabilities/default.json` is empty) and the splash page's CSP allows no inline code.

### Start-up check

The same check runs on any installation, from `engine\slai-root`:

```
..\runtime\python.exe -X utf8 -m applications.CADCon --check --verify-files [--require-agents] [--json]
```

It reports Python, SLAI's shared tools, the bundle manifest and (with `--verify-files`) a SHA-256 of every file, the web client, the format back ends, the agents' data files and each agent's health. Hard problems (missing SLAI, web client or modified files) fail it; missing optional parts are warnings.

## Developing

* `npm run app:dev` runs the shell against the **checkout** (debug build): it starts `python -m applications.CADCon` from the SLAI folder above CADCon, no bundle needed. Set `CADCON_DEV_PYTHON` to a Python (for example SLAI's `venv\Scripts\python.exe`) in a release build to do the same.
* The older linked launcher (`applications/tools/build/freeze_launcher.py`, which runs the checkout's venv) still works for developers and is unchanged.
* `npm run bundle:plan` shows what the installer would contain.

## Updating and uninstalling

The NSIS hooks (`src-tauri/installer/hooks.nsh`) delete `engine\` before installing and after uninstalling, so an update never leaves files of the old runtime next to the new one. Nothing under `%LOCALAPPDATA%\SLAI\CADCon` is touched. The version is kept in sync in `app.py`, `package.json`, `Cargo.toml` and `tauri.conf.json` (checked by `tests/test_desktop_config.py`). The version `1.0.0-dev` is a pre-release, which is why only NSIS is built (MSI rejects it, and also copes badly with tens of thousands of files).

## Verification status

Verified here (Linux sandbox, SLAI-shaped layout):

* the unit tests (`tests/`, including the new `test_desktop_config.py` and `test_bundle.py`) and the end-to-end tests (`tests_e2e/`);
* the engine's desktop flags and protocol lines (`--ready-line`, `--port-fallback`, `--exit-on-stdin-close`, error lines);
* the bundle builder's staging, import-closure check, manifest, hash verification and stale-file clean-up, with a stand-in interpreter: 1,282 SLAI files and 39 CADCon files were staged, the relocated tree started CADCon and passed `--check --verify-files`; the Spatial and Verification agents loaded from the relocated tree;
* the Rust sources type-check against hand-written stand-ins for the `tauri`, `serde` and `serde_json` APIs (the real crates were not available). This checks the launcher's own logic and borrowing, **not** its use of Tauri's real API; that part is copied from the working Shadow Sovereignty shell.

**Not verified** (needs Windows and network access):

* downloading the embeddable Python and the wheels, `pip install --target` for Windows, and whether every pinned version has a `win_amd64`/`cp313` wheel (IfcOpenShell in particular);
* that `runtime\python.exe` runs CADCon, and that the Reasoning agent loads with the real `torch` (it could not be loaded here: no torch);
* `cargo build`, `tauri build`, the NSIS installer, WebView2 behaviour, the installer hooks;
* installer and installed size (nothing was measured; torch alone is several hundred MB), first-start time, a clean-machine test, and antivirus behaviour on a large unsigned installer;
* DXF, IFC (and DWG) conversion, which need the optional back ends.

## Known limitations

* The installer is not code-signed; Windows SmartScreen will warn.
* No automatic updates; install a newer installer over the old one.
* If SLAI's shared tools are missing from a *development* run, the module-level imports fail before CADCon can print a protocol line; the shell then reports that the engine stopped (details in `engine.log`).
* Windows only. The bundle builder refuses to build the embedded runtime on other systems.
