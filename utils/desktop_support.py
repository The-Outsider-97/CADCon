"""What the installed desktop program needs from the application itself: the protocol's error line, the start-up check (``--check``) and the description
of the bundle the installer ships.

Nothing here imports SLAI at module level, so the error line and the check can still report when SLAI itself cannot be found.

The desktop protocol is SLAI's shared one (``applications/tools/desktop.py``): the window starts the engine with ``--ready-line``, reads one line of standard
output (``CADCON_READY {json}`` or ``CADCON_ERROR <message>``) and then opens the page.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

SLUG = "cadcon"
TITLE = "CADCon"
PRODUCT_NAME = "SLAI CADCon"
IDENTIFIER = "app.slai.cadcon"
#: The port the window prefers, so its web view keeps one address (and one browser storage) from run to run. Not the browser version's 8770.
DESKTOP_PORT = 47872
MIN_PYTHON = (3, 12)

BUNDLE_MANIFEST = "bundle-manifest.json"   # written next to the SLAI tree by tools/desktop/build_bundle.py
BUNDLE_SLAI_DIR = "slai-root"              # the SLAI tree inside an installed bundle: <engine>/slai-root
MANIFEST_FORMAT = 1

#: Files the three integrated SLAI agents read at run time (relative to the SLAI folder). They are the agents' own data: rules, graphs and a small strategy
#: model stored as JSON. No agent CADCon uses downloads or loads separate model weights.
AGENT_DATA = (
    "src/agents/reasoning/configs/reasoning_config.yaml",
    "src/agents/reasoning/models/strategy_selector/model.json",
    "src/agents/reasoning/templates/semantic_frames.json",
    "src/agents/reasoning/templates/structure_weights.json",
    "src/agents/spatial/configs/spatial_config.yaml",
    "src/agents/verification/configs/verification_config.yaml",
)


def prefix() -> str:
    return SLUG.upper().replace("-", "_")


def announce_error(message: str) -> None:
    """One line on standard output for the window to show; the line breaks of a longer message are folded into spaces (same format as the shared protocol)."""
    print(f"{prefix()}_ERROR " + " ".join(str(message).split()), flush=True)


# ------------------------------------------------------------------------------------------------------------------------------ the bundle
def bundle_manifest_path(slai_root: Path) -> Path:
    """Where an installed bundle keeps its manifest: beside ``slai-root``. Only a folder named ``slai-root`` is treated as a bundle."""
    return Path(slai_root).parent / BUNDLE_MANIFEST


def read_bundle_manifest(slai_root: Path) -> dict[str, Any] | None:
    """The bundle's manifest when ``slai_root`` is the SLAI tree of an installed bundle, else ``None`` (a development checkout has none)."""
    root = Path(slai_root)
    if root.name != BUNDLE_SLAI_DIR:
        return None
    path = bundle_manifest_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("format") == MANIFEST_FORMAT else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_bundle(manifest: dict[str, Any], engine_dir: Path, *, hasher: Callable[[Path], str] = file_sha256,
                  limit: int = 20) -> tuple[int, list[str]]:
    """Hash every file the manifest lists. Returns ``(files checked, problems)``; at most ``limit`` problems are spelled out."""
    problems: list[str] = []
    checked = 0
    missing = changed = 0
    for rel, row in sorted((manifest.get("files") or {}).items()):
        checked += 1
        path = Path(engine_dir) / rel
        if not path.is_file():
            missing += 1
            if len(problems) < limit:
                problems.append(f"missing: {rel}")
            continue
        if hasher(path) != row.get("sha256"):
            changed += 1
            if len(problems) < limit:
                problems.append(f"changed: {rel}")
    if missing + changed > len(problems):
        problems.append(f"... and {missing + changed - len(problems)} more ({missing} missing, {changed} changed in total)")
    return checked, problems


# ------------------------------------------------------------------------------------------------------------------------------ the check
OK, WARN, FAIL = "ok", "warning", "failed"


@dataclass
class Item:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.items.append(Item(name, status, detail))

    @property
    def ok(self) -> bool:
        return all(item.status != FAIL for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "items": [item.to_dict() for item in self.items]}

    def format(self) -> str:
        marks = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
        lines = [f"{TITLE} environment check"]
        lines += [f"  {marks[item.status]} {item.name}: {item.detail}" for item in self.items]
        lines.append("Result: " + ("ready to run" if self.ok else "NOT ready: fix the failed items above"))
        return "\n".join(lines)


def _first_line(text: str) -> str:
    return (str(text).splitlines() or [""])[0][:300]


def check_environment(*, slai_root: Path | None, app_dir: Path, client_dir: Path, config: Any = None, require_agents: bool = False,
                      probe_agents: bool = True, verify_files: bool = False, capabilities: Callable[[], dict[str, Any]] | None = None,
                      agent_health: Callable[[str], dict[str, Any]] | None = None, agent_keys: Iterable[str] = ()) -> Report:
    """Look at everything the application needs and say what is missing. Hard requirements (Python, SLAI's shared tools, the web client, a bundle that matches
    its manifest) fail the check. Optional parts (format back ends, SLAI agents) are warnings, because CADCon's deterministic features never depend on them;
    ``require_agents`` turns a missing agent into a failure (the installer build uses it, so a bundle without its agents is never shipped)."""
    report = Report()
    version = sys.version_info
    report.add("python", OK if version[:2] >= MIN_PYTHON else FAIL,
               f"{platform.python_implementation()} {platform.python_version()} ({platform.machine() or 'unknown'}) at {sys.executable}"
               + ("" if version[:2] >= MIN_PYTHON else f"; CADCon needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer"))
    if slai_root is None:
        report.add("slai", FAIL, "SLAI's shared tools were not found. Set SLAI_ROOT to the SLAI folder (an installed program carries its own).")
        return report
    shared = [p for p in ("src/utils/sldc.py", "applications/tools/server/http.py", "applications/tools/desktop.py") if not (slai_root / p).is_file()]
    report.add("slai", FAIL if shared else OK, f"{slai_root}" + (f"; missing {', '.join(shared)}" if shared else ""))

    manifest = read_bundle_manifest(slai_root)
    if manifest is None:
        report.add("bundle", OK, "development checkout (no installer bundle)")
    else:
        app = manifest.get("app") or {}
        report.add("bundle", OK, f"{app.get('name', TITLE)} {app.get('version', '?')}, built {manifest.get('built', '?')} from SLAI "
                                 f"{(manifest.get('slai') or {}).get('revision') or 'of unknown revision'}; {len(manifest.get('files') or {}):,} files")
        if verify_files:
            checked, problems = verify_bundle(manifest, bundle_manifest_path(slai_root).parent)
            report.add("bundle files", FAIL if problems else OK,
                       f"{checked:,} files hashed, all match the manifest" if not problems else "; ".join(problems))
    index = Path(client_dir) / "index.html"
    report.add("web client", OK if index.is_file() else FAIL,
               f"{client_dir}" if index.is_file() else f"{index} is missing (run 'npm run build' in the CADCon folder)")
    if index.is_file() and not (Path(client_dir) / "startup" / "loader.js").is_file():
        report.add("start-up screen", WARN, "startup/loader.js is not in the web client; the page would start without the shared start-up screen")

    if capabilities is not None:
        try:
            formats = capabilities().get("formats", {})
            usable = sorted(k for k, v in formats.items() if v.get("available"))
            missing = sorted(k for k, v in formats.items() if not v.get("available"))
            report.add("formats", OK if usable else FAIL, "ready: " + (", ".join(usable) or "none")
                       + ("" if not missing else f"; unavailable here: {', '.join(missing)}"))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            report.add("formats", FAIL, f"the format table could not be built: {type(exc).__name__}: {_first_line(exc)}")

    present = [p for p in AGENT_DATA if (slai_root / p).is_file()]
    lost = [p for p in AGENT_DATA if p not in present]
    report.add("agent data", OK if not lost else (FAIL if require_agents else WARN),
               f"{len(present)} of {len(AGENT_DATA)} files the integrated agents read are present" + (f"; missing {', '.join(lost)}" if lost else "")
               + " (no separate model weights are used)")
    if agent_health is not None:
        for key in agent_keys:
            try:
                row = agent_health(key)
            except Exception as exc:  # noqa: BLE001
                report.add(f"agent {key}", FAIL if require_agents else WARN, f"{type(exc).__name__}: {_first_line(exc)}")
                continue
            state = row.get("state")
            if state == "ready":
                report.add(f"agent {key}", OK, f"{row.get('class') or key} loaded")
            elif state == "disabled":
                report.add(f"agent {key}", OK, "switched off in the configuration")
            elif state == "not_loaded" and not probe_agents:
                report.add(f"agent {key}", OK, "not probed (agents load on first use)")
            else:
                report.add(f"agent {key}", FAIL if require_agents else WARN, _first_line(row.get("reason") or f"state {state}"))
    return report

