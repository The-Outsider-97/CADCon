#!/usr/bin/env python3
"""Build the folder the SLAI CADCon installer ships: SLAI itself, CADCon, and a Python runtime with everything they need, in one self-contained tree.

    python tools/desktop/build_bundle.py --slai-root ..\\..                      # into src-tauri/engine, profile "standard"
    python tools/desktop/build_bundle.py --slai-root ..\\.. --profile full       # also everything else SLAI imports that fits a desktop program
    python tools/desktop/build_bundle.py --slai-root ..\\.. --plan               # only say what would be copied and how big it is

The result (``engine/``)::

    runtime/               embeddable CPython + site-packages (numpy, scipy, torch (CPU), pgmpy, trimesh, ...)
    slai-root/             a SLAI tree: src/ (agents and their models), checkpointing/, logs/, applications/tools, applications/assets
      applications/CADCon/   CADCon itself, laid out exactly as in a SLAI checkout so the same code runs unchanged
    bundle-manifest.json   versions, what was installed, and a SHA-256 for every file (``--check --verify-files`` re-checks them)
    THIRD_PARTY_NOTICES.txt

Why a copy of SLAI and not a link to the checkout: an installed program must not depend on a folder somewhere else on the disk. The older "linked" launcher
(applications/tools/build/freeze_launcher.py) is still available for developers; this is the self-contained one.

Standard library only. Run it on Windows (the interpreter is Windows' and the self-check runs it). On another system ``--plan`` and ``--skip-runtime``
work, and ``--runtime-python`` stands in for the embedded interpreter, which is how the layout is tested without Windows.

The runtime needs network access to python.org (the embeddable Python) and to PyPI / download.pytorch.org (wheels), unless ``--python-embed`` and a
local wheel source are given. Nothing is installed on the build machine: wheels go into the bundle only, as ``pip install --target``.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent.parent
SPEC_PATH = APP_DIR / "desktop" / "bundle-spec.json"

MANIFEST = "bundle-manifest.json"
NOTICES = "THIRD_PARTY_NOTICES.txt"
MARKER = ".cadcon-bundle"            # present in every folder this script made, so that it only ever cleans its own output
SLAI_DIR = "slai-root"
RUNTIME_DIR = "runtime"
APP_SLOT = Path("applications") / "CADCon"
MANIFEST_FORMAT = 1


class BuildError(Exception):
    """Something the person building must fix; the message says what."""


# --------------------------------------------------------------------------------------------------------------------------------- matching
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Matcher:
    """Glob patterns over '/'-separated relative paths. ``**/`` matches any depth (including none); a match on a folder covers everything below it."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = [_glob_regex(p) for p in patterns]

    def __call__(self, rel: str) -> bool:
        return any(p.match(rel) for p in self.patterns)


def iter_files(root: Path, include: Iterable[str], exclude: Iterable[str]) -> Iterator[tuple[str, Path]]:
    """``(relative path, absolute path)`` of every file under ``root`` that is below an ``include`` entry and not excluded, in a stable order."""
    skip = Matcher(exclude)
    for entry in include:
        base = root / entry
        if not base.exists():
            continue
        if base.is_file():
            if not skip(entry):
                yield entry, base
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            rel_dir = Path(current).relative_to(root).as_posix()
            dirs[:] = sorted(d for d in dirs if not skip(f"{rel_dir}/{d}"))
            for name in sorted(files):
                rel = f"{rel_dir}/{name}"
                if not skip(rel):
                    yield rel, Path(current) / name


def copy_files(files: Iterable[tuple[str, Path]], dest: Path) -> tuple[int, int]:
    """Copy into ``dest`` keeping relative paths. Returns ``(files, bytes)``."""
    count = size = 0
    for rel, source in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
        size += target.stat().st_size
    return count, size


# ----------------------------------------------------------------------------------------------------------------------------------- inputs
def load_spec(path: Path = SPEC_PATH) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{path} could not be read: {exc}") from exc
    if spec.get("format") != MANIFEST_FORMAT:
        raise BuildError(f"{path} has format {spec.get('format')!r}; this script reads format {MANIFEST_FORMAT}")
    return spec


def check_sources(slai_root: Path, app_dir: Path, spec: dict) -> None:
    """Refuse early, naming everything that is missing, instead of failing half way through a long build."""
    problems = []
    for rel in spec["slai"]["required"]:
        if not (slai_root / rel).is_file():
            problems.append(f"SLAI folder {slai_root}: {rel} is missing")
    for rel in spec["app"]["required"]:
        if not (app_dir / rel).is_file():
            hint = " (run 'npm run build' and 'npm run splash' in the CADCon folder first)" if rel.startswith("dist/") else ""
            problems.append(f"CADCon folder {app_dir}: {rel} is missing{hint}")
    if problems:
        raise BuildError("The bundle cannot be built yet:\n  " + "\n  ".join(problems))


def app_version(app_dir: Path) -> str:
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', (app_dir / "app.py").read_text(encoding="utf-8"), re.M)
    if not match:
        raise BuildError("VERSION was not found in app.py")
    return match.group(1)


def git_revision(path: Path) -> dict:
    """The commit of a SLAI checkout and whether it has uncommitted changes. Empty values when it is not a git checkout (nothing is invented)."""
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None
    revision = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no") if revision else None
    return {"revision": revision, "uncommitted_changes": bool(status) if status is not None else None}


# ------------------------------------------------------------------------------------------------------------------------------------ staging
def prepare_output(out: Path, *, clean: bool = True) -> None:
    """Make ``out`` an empty folder. An existing folder is only emptied when this script made it (the marker file), so a wrong ``--out`` cannot do harm."""
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER).is_file():
            raise BuildError(f"{out} exists, is not empty and was not made by this script; choose an empty folder or remove it yourself.")
        if clean:
            shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("made by tools/desktop/build_bundle.py; this folder is rebuilt from scratch on every run\n", encoding="utf-8")


def stage_slai(slai_root: Path, spec: dict, out: Path) -> tuple[int, int]:
    return copy_files(iter_files(slai_root, spec["slai"]["include"], spec["slai"]["exclude"]), out / SLAI_DIR)


def stage_app(app_dir: Path, spec: dict, out: Path) -> tuple[int, int]:
    return copy_files(iter_files(app_dir, spec["app"]["include"], spec["app"]["exclude"]), out / SLAI_DIR / APP_SLOT)


def check_staged(out: Path, spec: dict) -> None:
    missing = [rel for rel in spec["slai"]["required"] if not (out / SLAI_DIR / rel).is_file()]
    missing += [f"{APP_SLOT.as_posix()}/{rel}" for rel in spec["app"]["required"] if not (out / SLAI_DIR / APP_SLOT / rel).is_file()]
    if missing:
        raise BuildError("The staged tree is incomplete (an exclude pattern removed something required?): " + ", ".join(missing))


# ------------------------------------------------------------------------------------------------------------------------- reachability
def local_closure(root: Path, entries: Iterable[str]) -> set[str]:
    """The files of ``root`` that ``entries`` import, found by reading every import statement (also those in functions and try/except), transitively.
    Imports of things outside ``root`` are ignored. Because lazy imports count, this is a superset of what runs, which is the safe side for a bundle."""
    import ast
    top = {p.name for p in root.iterdir() if p.is_dir()} | {p.stem for p in root.glob("*.py")}
    seen: dict[Path, str] = {}

    def locate(name: str) -> tuple[Path, str] | None:
        parts = name.split(".")
        for n in range(len(parts), 0, -1):
            base = root.joinpath(*parts[:n])
            if base.with_suffix(".py").is_file():
                return base.with_suffix(".py"), ".".join(parts[:n])
            if (base / "__init__.py").is_file():
                return base / "__init__.py", ".".join(parts[:n])
        return None

    def visit(name: str) -> None:
        found = locate(name)
        if found is None or found[0] in seen:
            return
        path, canonical = found
        seen[path] = canonical
        parts = canonical.split(".")
        for i in range(1, len(parts)):
            visit(".".join(parts[:i]))                       # importing a module runs its parent packages
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return
        is_package = path.name == "__init__.py"
        here = parts if is_package else parts[:-1]
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = here[: len(here) - (node.level - 1)] if node.level > 1 else here
                    prefix = ".".join(base + ([node.module] if node.module else []))
                else:
                    prefix = node.module or ""
                names = [prefix] + [f"{prefix}.{a.name}" for a in node.names] if prefix else []
            for target in names:
                if target.split(".")[0] in top:
                    visit(target)

    for entry in entries:
        visit(entry)
    return {path.relative_to(root).as_posix() for path in seen}


def check_reachable(slai_root: Path, out: Path, spec: dict) -> tuple[int, list[str]]:
    """Every SLAI file the entry modules can import must have been copied: an exclude pattern must not remove code that is used.

    Returns ``(files reachable, warnings)``. A missing file inside a folder the bundle ships (src, checkpointing, logs, applications) is an error. Code in
    folders the bundle does not ship at all (data, deployment, modules, ...) is only a warning, because SLAI imports some of it lazily and optionally;
    the self-check at the end of the build, which loads the agents inside the bundle, is what decides whether the agents really work without it."""
    entries = spec["slai"]["entry_modules"]
    needed = local_closure(slai_root, [e for e in entries if not e.startswith("applications.CADCon")])
    staged = local_closure(out / SLAI_DIR, entries)
    shipped = tuple(Path(i).parts[0] + "/" for i in spec["slai"]["include"])
    lost = sorted(rel for rel in needed if rel not in staged and not (out / SLAI_DIR / rel).is_file())
    hard = [rel for rel in lost if rel.startswith(shipped)]
    soft = sorted({rel.split("/", 1)[0] for rel in lost if not rel.startswith(shipped)})
    if hard:
        shown = ", ".join(hard[:15]) + (f" and {len(hard) - 15} more" if len(hard) > 15 else "")
        raise BuildError(f"{len(hard)} SLAI files that the entry modules import were left out of the bundle by the exclude patterns in bundle-spec.json: {shown}")
    warnings = [f"the entry modules can import code from {', '.join(soft)}, which the bundle does not ship; if an agent needs it, the self-check will say so"] if soft else []
    return len(staged), warnings


# ----------------------------------------------------------------------------------------------------------------------------------- runtime
PIP_TARGET_FLAGS = ("--platform", "win_amd64", "--implementation", "cp")


def target_flags(series: str) -> list[str]:
    """Always resolve for the interpreter that will run the bundle (Windows, CPython ``series``), whatever Python runs this script."""
    major_minor = series.replace(".", "")
    return [*PIP_TARGET_FLAGS, "--python-version", series, "--abi", f"cp{major_minor}"]


def embed_url(spec: dict, version: str) -> str:
    return spec["runtime"]["embed_url"].format(version=version)


def resolve_python_version(spec: dict, requested: str | None) -> str:
    series = spec["runtime"]["python_series"]
    version = requested or (platform.python_version() if platform.python_version().startswith(series + ".") else None)
    if not version:
        raise BuildError(f"Say which Python {series} release to embed, for example --python-version {series}.14 (this script runs on Python "
                         f"{platform.python_version()}, which does not match the bundle's {series}).")
    if not version.startswith(series + "."):
        raise BuildError(f"--python-version {version} does not belong to the series {series} the wheels are resolved for (bundle-spec.json).")
    return version


def fetch_embeddable(url: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / url.rsplit("/", 1)[-1]
    if target.is_file() and zipfile.is_zipfile(target):
        return target
    print(f"downloading {url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as handle:
            shutil.copyfileobj(response, handle)
    except (OSError, urllib.error.URLError) as exc:
        target.unlink(missing_ok=True)
        raise BuildError(f"The embeddable Python could not be downloaded from {url} ({exc}). Download it yourself and pass --python-embed <zip>.") from exc
    if not zipfile.is_zipfile(target):
        target.unlink(missing_ok=True)
        raise BuildError(f"{url} did not return a zip file.")
    return target


def install_embeddable(archive: Path, runtime: Path, series: str) -> Path:
    """Unpack the embeddable Python and rewrite its path file; returns the site-packages folder."""
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if "python.exe" not in names:
            raise BuildError(f"{archive} is not an embeddable Python for Windows (no python.exe inside).")
        z.extractall(runtime)
    pth = list(runtime.glob("python*._pth"))
    if len(pth) != 1:
        raise BuildError(f"Expected exactly one python*._pth in {archive}, found {[p.name for p in pth]}.")
    stem = pth[0].stem                                              # python313
    if not stem.startswith("python" + series.replace(".", "")):
        raise BuildError(f"{archive} is Python {stem[6:]}, the bundle's wheels are for {series}.")
    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    # The embeddable Python ignores PYTHONPATH and the current folder: this file is the whole search path. SLAI's folder is on it so that
    # `python -m applications.CADCon` works from anywhere, as in a checkout. 'import site' lets sitecustomize (below) run.
    pth[0].write_text("\n".join([f"{stem}.zip", ".", "Lib\\site-packages", f"..\\{SLAI_DIR}", "import site", ""]), encoding="utf-8")
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return site


SITECUSTOMIZE = '''"""Written by tools/desktop/build_bundle.py. The embeddable Python does not process the .pth files of site-packages on its own; some packages ship one."""
import os
import site

site.addsitedir(os.path.dirname(os.path.abspath(__file__)))
'''


def pip_command(python: str, target: Path, requirements: Iterable[Path] = (), packages: Iterable[str] = (), *, series: str, torch_index: str | None,
                constraints: Path | None) -> list[str]:
    cmd = [python, "-m", "pip", "install", "--target", str(target), "--only-binary=:all:", "--disable-pip-version-check", "--no-warn-script-location",
           *target_flags(series)]
    if torch_index:
        cmd += ["--extra-index-url", torch_index]
    if constraints:
        cmd += ["--constraint", str(constraints)]
    for req in requirements:
        cmd += ["-r", str(req)]
    return [*cmd, *packages]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, **kwargs)


def requirement_lines(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.split("#", 1)[0].strip()]


def install_wheels(site: Path, spec: dict, profile: str, series: str, constraints: Path | None, app_dir: Path = APP_DIR) -> None:
    files = [app_dir / rel for rel in spec["runtime"]["profiles"][profile]]
    done = run(pip_command(sys.executable, site, files, series=series, torch_index=spec["runtime"]["torch_index"], constraints=constraints))
    if done.returncode != 0:
        raise BuildError("pip could not install the required packages (see its output above). Nothing is left out silently: fix the cause and run again.")


def install_optional(site: Path, spec: dict, series: str, constraints: Path | None, app_dir: Path = APP_DIR) -> dict[str, dict]:
    """Each optional package on its own, into a scratch folder that is then merged without replacing anything already installed. Returns a status per package."""
    results: dict[str, dict] = {}
    for line in requirement_lines(app_dir / spec["runtime"]["optional"]):
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0]
        with tempfile.TemporaryDirectory() as scratch:
            done = run(pip_command(sys.executable, Path(scratch), packages=[line], series=series, torch_index=None, constraints=constraints),
                       capture_output=True, text=True)
            if done.returncode != 0:
                reason = ([l for l in (done.stderr or "").splitlines() if l.strip()] or ["pip failed"])[-1][:300]
                results[name] = {"status": "unavailable", "reason": reason}
                print(f"WARNING: {name} could not be installed ({reason}); CADCon will list what needs it as unavailable.", flush=True)
                continue
            merge_tree(Path(scratch), site)
            results[name] = {"status": "installed", "version": installed_versions(Path(scratch)).get(normalize(name))}
    return results


def merge_tree(source: Path, dest: Path) -> None:
    """Copy ``source`` into ``dest``; a file or folder that already exists in ``dest`` stays as it is (the required packages win)."""
    for entry in source.iterdir():
        target = dest / entry.name
        if entry.is_dir() and target.is_dir():
            merge_tree(entry, target)
        elif not target.exists():
            shutil.copytree(entry, target) if entry.is_dir() else shutil.copy2(entry, target)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_versions(site: Path) -> dict[str, str]:
    """``{normalized name: version}`` from the ``*.dist-info`` folders of a site-packages folder."""
    found: dict[str, str] = {}
    for info in site.glob("*.dist-info"):
        meta = info / "METADATA"
        if not meta.is_file():
            continue
        name = version = None
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:") and name is None:
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:") and version is None:
                version = line.split(":", 1)[1].strip()
            elif not line.strip():
                break
        if name and version:
            found[normalize(name)] = version
    return found


def prune(runtime: Path, patterns: Iterable[str]) -> int:
    """Remove build-time-only files from site-packages (headers, test suites, caches). Returns how many entries were removed."""
    site = runtime / "Lib" / "site-packages"
    match = Matcher(patterns)
    removed = 0
    for current, dirs, files in os.walk(site):
        rel_dir = Path(current).relative_to(site).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        for d in list(dirs):
            if match(rel_dir + d):
                shutil.rmtree(Path(current) / d, ignore_errors=True)
                dirs.remove(d)
                removed += 1
        for f in files:
            if match(rel_dir + f):
                (Path(current) / f).unlink(missing_ok=True)
                removed += 1
    return removed


def compile_all(python: Path, folders: Iterable[Path]) -> None:
    """Byte-compile with the bundle's own interpreter so the first start does not pay for it. A failure is reported, not fatal (the code still runs)."""
    for folder in folders:
        done = run([str(python), "-m", "compileall", "-q", "-j", "0", str(folder)], capture_output=True, text=True)
        if done.returncode != 0:
            print(f"WARNING: byte-compiling {folder} reported problems (the code still runs, but starts more slowly):\n{(done.stdout or '')[-600:]}", flush=True)


# -------------------------------------------------------------------------------------------------------------------------------- manifest
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(out: Path) -> dict[str, dict]:
    """Every file below ``out`` except caches, this script's marker and the manifest itself."""
    files: dict[str, dict] = {}
    skip = {MANIFEST, MARKER}
    for current, dirs, names in os.walk(out):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            path = Path(current) / name
            rel = path.relative_to(out).as_posix()
            if rel in skip:
                continue
            files[rel] = {"sha256": sha256(path), "size": path.stat().st_size}
    return files


def license_of(meta: Path) -> str:
    expression = classifier = plain = None
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            break
        if line.startswith("License-Expression:"):
            expression = line.split(":", 1)[1].strip()
        elif line.startswith("License:") and not plain:
            plain = line.split(":", 1)[1].strip()
        elif line.startswith("Classifier: License ::") and not classifier:
            classifier = line.split("::")[-1].strip()
    text = expression or classifier or plain or "not stated in the package metadata"
    return " ".join(text.split())[:120]


def write_notices(out: Path, site: Path | None) -> int:
    lines = ["Third-party software in this program",
             "",
             "SLAI CADCon ships Python and the packages below as unmodified files. Each package's own licence text is in its *.dist-info folder in",
             "runtime/Lib/site-packages. Components under the LGPL (for example IfcOpenShell, when present) stay ordinary replaceable files.",
             "Python itself: runtime/ (see LICENSE.txt there).",
             ""]
    count = 0
    if site is not None and site.is_dir():
        for info in sorted(site.glob("*.dist-info")):
            meta = info / "METADATA"
            if meta.is_file():
                name, _, version = info.name[: -len(".dist-info")].rpartition("-")
                lines.append(f"{name} {version}: {license_of(meta)}")
                count += 1
    (out / NOTICES).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def write_manifest(out: Path, *, spec: dict, profile: str, version: str, slai_root: Path, python_version: str | None, optional: dict[str, dict],
                   package_versions: dict[str, str], staged: dict[str, int]) -> dict:
    files = hash_tree(out)
    manifest = {
        "format": MANIFEST_FORMAT,
        "app": {"name": "CADCon", "slug": "cadcon", "version": version},
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": profile,
        "slai": git_revision(slai_root),
        "python": python_version,
        "packages": dict(sorted(package_versions.items())),
        "optional_packages": optional,
        "staged": staged,
        "total_bytes": sum(row["size"] for row in files.values()),
        "files": files,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def remove_run_artifacts(out: Path, manifest: dict) -> int:
    """Delete what running the self-check left in the bundle (SLAI writes logs and snapshots relative to its working folder): every file the manifest does
    not list, except byte-code caches, and the folders that leaves empty. Returns how many files were removed, so the installer ships what was hashed."""
    listed = set(manifest["files"]) | {MANIFEST, MARKER}
    removed = 0
    for current, dirs, names in os.walk(out):
        if Path(current).name == "__pycache__":
            dirs[:] = []
            continue
        for name in names:
            path = Path(current) / name
            if path.relative_to(out).as_posix() not in listed:
                path.unlink()
                removed += 1
    for current, _dirs, _names in os.walk(out, topdown=False):
        if Path(current) != out and not any(Path(current).iterdir()):
            Path(current).rmdir()
    return removed


# ------------------------------------------------------------------------------------------------------------------------------------ check
def check_command(out: Path, python: Path | None, *, embedded: bool, require_agents: bool, verify_files: bool) -> tuple[list[str], dict[str, str], Path]:
    """How to run CADCon's own environment check inside the bundle. The embedded interpreter finds everything through its path file; a stand-in
    interpreter (``--runtime-python``) is pointed at the SLAI folder the same way, with the current folder left off the search path."""
    slai = out / SLAI_DIR
    exe = python if python is not None else out / RUNTIME_DIR / "python.exe"
    cmd = [str(exe)] + ([] if embedded else ["-P"]) + ["-X", "utf8", "-m", "applications.CADCon", "--check", "--no-browser"]
    if require_agents:
        cmd.append("--require-agents")
    if verify_files:
        cmd.append("--verify-files")
    env = dict(os.environ, SLAI_ROOT=str(slai))
    if not embedded:
        env["PYTHONPATH"] = str(slai)
    return cmd, env, slai


def run_check(out: Path, python: Path | None, *, embedded: bool, require_agents: bool = True, verify_files: bool = True) -> int:
    cmd, env, cwd = check_command(out, python, embedded=embedded, require_agents=require_agents, verify_files=verify_files)
    done = run(cmd, env=env, cwd=cwd)
    return done.returncode


# --------------------------------------------------------------------------------------------------------------------------------------- main
def interpreter_version(python: Path) -> str | None:
    try:
        done = subprocess.run([str(python), "-c", "import platform;print(platform.python_version())"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slai-root", required=True, help="the SLAI folder to copy (the checkout CADCon sits in)")
    ap.add_argument("--app-dir", default=str(APP_DIR), help="the CADCon folder (default: this one)")
    ap.add_argument("--out", help="where the bundle goes (default: <CADCon>/src-tauri/engine)")
    ap.add_argument("--profile", choices=("standard", "full"), default="standard")
    ap.add_argument("--python-version", help="the Python release to embed, e.g. 3.13.14 (default: the one running this script, if it is the right series)")
    ap.add_argument("--python-embed", help="a python-X.Y.Z-embed-amd64.zip you downloaded, instead of downloading it")
    ap.add_argument("--constraints", help="a pip constraints file (default: desktop/constraints.txt when it exists)")
    ap.add_argument("--skip-optional", action="store_true", help="do not try the optional geometry back ends (ezdxf, ifcopenshell)")
    ap.add_argument("--skip-runtime", action="store_true", help="copy SLAI and CADCon only (no Python runtime)")
    ap.add_argument("--runtime-python", help="use this interpreter for the self-check instead of an embedded one (implies --skip-runtime; for testing the layout)")
    ap.add_argument("--no-compile", action="store_true", help="do not byte-compile the bundle")
    ap.add_argument("--no-verify", action="store_true", help="skip the self-check at the end (an installer built this way has not been checked)")
    ap.add_argument("--allow-missing-agents", action="store_true", help="the self-check treats an agent that cannot load as a warning instead of a failure")
    ap.add_argument("--plan", action="store_true", help="only list what would be copied and its size")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return build(args)
    except BuildError as exc:
        print(f"\nThe bundle was not built: {exc}", file=sys.stderr)
        return 2


def build(args: argparse.Namespace) -> int:
    spec = load_spec()
    slai_root, app_dir = Path(args.slai_root).resolve(), Path(args.app_dir).resolve()
    out = Path(args.out).resolve() if args.out else app_dir / "src-tauri" / "engine"
    check_sources(slai_root, app_dir, spec)
    version = app_version(app_dir)

    if args.plan:
        for label, root, section in (("SLAI", slai_root, spec["slai"]), ("CADCon", app_dir, spec["app"])):
            files = list(iter_files(root, section["include"], section["exclude"]))
            total = sum(path.stat().st_size for _, path in files)
            print(f"{label}: {len(files):,} files, {human(total)}")
        print(f"Runtime (not measured by --plan): Python {spec['runtime']['python_series']} and the packages of profile '{args.profile}'")
        return 0

    embedded = not (args.skip_runtime or args.runtime_python)
    if embedded and sys.platform != "win32":
        raise BuildError("The embedded Python runtime is built on Windows (its self-check runs python.exe). Use --skip-runtime or --runtime-python elsewhere.")
    constraints = Path(args.constraints) if args.constraints else (app_dir / "desktop" / "constraints.txt")
    constraints = constraints if constraints.is_file() else None
    if args.constraints and constraints is None:
        raise BuildError(f"--constraints {args.constraints} does not exist")

    prepare_output(out)
    staged = {}
    staged["slai_files"], staged["slai_bytes"] = stage_slai(slai_root, spec, out)
    staged["app_files"], staged["app_bytes"] = stage_app(app_dir, spec, out)
    check_staged(out, spec)
    reachable, reach_warnings = check_reachable(slai_root, out, spec)
    print(f"staged SLAI: {staged['slai_files']:,} files, {human(staged['slai_bytes'])}; CADCon: {staged['app_files']:,} files, {human(staged['app_bytes'])}; "
          f"{reachable:,} files are reachable from the entry modules, none of them left out", flush=True)
    for warning in reach_warnings:
        print(f"WARNING: {warning}", flush=True)

    python_version, optional, packages, site = None, {}, {}, None
    python_exe: Path | None = Path(args.runtime_python).resolve() if args.runtime_python else None
    if embedded:
        series = spec["runtime"]["python_series"]
        python_version = resolve_python_version(spec, args.python_version)
        archive = Path(args.python_embed).resolve() if args.python_embed else fetch_embeddable(embed_url(spec, python_version), app_dir / "build" / "cache")
        site = install_embeddable(archive, out / RUNTIME_DIR, series)
        install_wheels(site, spec, args.profile, series, constraints, app_dir)
        if not args.skip_optional:
            optional = install_optional(site, spec, series, constraints, app_dir)
        removed = prune(out / RUNTIME_DIR, spec["runtime"]["prune"])
        print(f"pruned {removed:,} build-time-only entries from site-packages", flush=True)
        packages = installed_versions(site)
        python_exe = out / RUNTIME_DIR / "python.exe"
        if not args.no_compile:
            compile_all(python_exe, [out / SLAI_DIR, site])
    else:
        python_version = interpreter_version(python_exe) if python_exe else None
    count = write_notices(out, site)
    manifest = write_manifest(out, spec=spec, profile=args.profile, version=version, slai_root=slai_root, python_version=python_version, optional=optional,
                              package_versions=packages, staged=staged)
    print(f"bundle: {len(manifest['files']):,} files, {human(manifest['total_bytes'])}; {count} third-party packages listed in {NOTICES}", flush=True)
    for name, row in optional.items():
        print(f"  optional {name}: {row['status']}" + (f" ({row.get('version')})" if row["status"] == "installed" else f" - {row['reason']}"), flush=True)

    if args.no_verify or (python_exe is None):
        print("\nSelf-check NOT run" + (" (no interpreter available)." if python_exe is None else " (--no-verify)."), "This bundle has not been verified.", flush=True)
        return 0
    status = run_check(out, python_exe, embedded=embedded, require_agents=not args.allow_missing_agents)
    if status != 0:
        raise BuildError("The bundle's own check failed (see the report above). The installer would not work as intended, so it was not accepted.")
    cleaned = remove_run_artifacts(out, manifest)
    agents = ("the bundle starts CADCon and its files match the manifest; the agents were allowed to be missing (--allow-missing-agents), see the "
              "report above for which loaded" if args.allow_missing_agents else
              "the bundle starts CADCon, its files match the manifest and the Spatial, Reasoning and Verification agents load")
    print(f"\nSelf-check passed: {agents}. ({cleaned} file{'s' if cleaned != 1 else ''} the check itself wrote were removed again.)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
