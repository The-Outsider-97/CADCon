"""CADCon's typed configuration facade.

``configs/cadcon_config.yaml`` is only a *source*. This module reads it through SLAI's shared ``ConfigRepository`` (safe YAML, duplicate-key
detection, mtime-aware cache - ``src.utils.configuration.bind_config``), then validates every value against the schema below and returns an
immutable :class:`CADConConfig`. Nothing here parses YAML itself.

Precedence, highest first::

    explicit overrides (CLI / tests / API)  >  supported environment variables  >  cadcon_config.yaml  >  built-in defaults

Only the environment variables named in :data:`SCHEMA` (column ``env``) are read; no arbitrary variable becomes a setting. ``server.host``
has no override at all and must stay on the loopback interface. Every setting has a documented meaning, a type, a default, a range or
choice list, and a runtime consumer (listed in ``CONSUMERS`` for the diagnostics endpoint and checked by the tests). Unknown keys are
errors - a typo never silently falls back to a default - and an invalid reload leaves the previous valid configuration active.
"""
from __future__ import annotations

import difflib
import math
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .cadcon_errors import ConfigurationError

HERE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = HERE / "configs" / "cadcon_config.yaml"
CONFIG_ENV = "CADCON_CONFIG"
SCHEMA_VERSION = 1

POLICIES = ("strict", "lossless_where_supported", "allow_approximation", "best_effort")
REPAIR_MODES = ("conservative", "balanced", "exploratory")
REPAIR_STRATEGIES = ("topology", "tolerant_weld", "isolate_shells", "hole_fill", "planar_cap")
APP_MODES = ("development", "production")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: Agents that have a CADCon adapter. Only these can be switched or routed; any other identifier is rejected.
INTEGRATED_AGENTS = ("spatial", "reasoning", "verification")
#: Workflow -> the agents that can serve it (a workflow list may only name agents from its tuple).
WORKFLOWS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "repair_advice": ("reasoning",),
    "analysis_interpretation": ("reasoning",),
    "candidate_postconditions": ("verification",),
    "compare_registration": ("spatial",),
})
DEFAULT_WORKFLOW_ROUTES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "repair_advice": ("reasoning",),
    "analysis_interpretation": ("reasoning",),
    "candidate_postconditions": ("verification",),
    "compare_registration": ("spatial",),
})


@dataclass(frozen=True)
class Setting:
    path: str
    kind: str                                   # bool | int | float | str | path | choice | strategies
    default: Any
    doc: str
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    nullable: bool = False
    env: str | None = None
    consumer: str = ""


def _s(path: str, kind: str, default: Any, doc: str, consumer: str, **kw: Any) -> Setting:
    return Setting(path, kind, default, doc, consumer=consumer, **kw)


MiB = 1024 * 1024
SCHEMA: tuple[Setting, ...] = (
    _s("application.name", "str", "CADCon", "Product name shown by /api/health and in the System workspace.", "app.health"),
    _s("application.schema_version", "int", SCHEMA_VERSION, "Configuration schema version; must equal the version CADCon implements.", "config_loader",
       low=SCHEMA_VERSION, high=SCHEMA_VERSION),
    _s("application.mode", "choice", "production", "development exposes the configuration file path in diagnostics; production shows only its name.",
       "app.system", choices=APP_MODES, env="CADCON_MODE"),
    _s("application.startup_diagnostics", "bool", True, "Print a capability and agent summary to the console when CADCon starts.", "app.main"),

    _s("server.host", "str", "127.0.0.1", "Interface to listen on. Must be loopback; there is no override for it.", "app.main", choices=LOOPBACK_HOSTS),
    _s("server.port", "int", 8770, "Preferred port (0 lets the OS choose).", "app.main", low=0, high=65535, env="CADCON_PORT"),
    _s("server.port_fallback", "bool", True, "When the preferred port is busy, let the OS choose a free one (an explicit --port stays strict).", "app.main"),
    _s("server.open_browser", "bool", True, "Open the application in the default browser after start-up.", "app.main", env="CADCON_OPEN_BROWSER"),
    _s("server.max_request_bytes", "int", 64 * MiB, "Largest request body the server accepts; must not be below files.max_source_bytes.", "app.main",
       low=1 * MiB, high=1024 * MiB),

    _s("paths.data_dir", "path", None, "Private data folder for uploads and outputs; null uses SLAI's per-user default.", "app.main",
       nullable=True, env="CADCON_DATA_DIR"),

    _s("files.max_source_bytes", "int", 64 * MiB, "Largest accepted CAD source file.", "engine, app.upload", low=1 * MiB, high=512 * MiB),
    _s("files.max_active_uploads", "int", 200, "Imported models kept at once; remove one to import another.", "app.upload", low=1, high=10_000),

    _s("geometry.default_unit", "str", "mm", "Declared length unit for formats without units (STL, OBJ) when a request names none.", "app.routes, engine",
       env="CADCON_DEFAULT_UNIT"),
    _s("geometry.max_triangles", "int", 600_000, "Largest triangle mesh CADCon will analyse in memory.", "engine", low=1_000, high=5_000_000),

    _s("conversion.default_policy", "choice", "best_effort", "Conversion quality policy used when a request names none.", "app.convert", choices=POLICIES),
    _s("conversion.dwg_executable", "path", None, "Absolute path of the ODA File Converter (enables DWG import/export); null disables DWG.", "app.main",
       nullable=True, env="CADCON_DWG_EXECUTABLE"),

    _s("repair.mode", "choice", "balanced", "Repair policy: conservative, balanced or exploratory (see the repair documentation).", "repair.policy",
       choices=REPAIR_MODES, env="CADCON_REPAIR_MODE"),
    _s("repair.max_candidates", "int", 5, "Most repair candidates generated per request.", "app.repair", low=1, high=5),
    _s("repair.large_mesh_triangles", "int", 200_000, "Meshes with more triangles than this use the reduced candidate limit.", "app.repair", low=1_000, high=5_000_000),
    _s("repair.large_mesh_max_candidates", "int", 2, "Candidate limit for meshes above repair.large_mesh_triangles.", "app.repair", low=1, high=5),
    _s("repair.max_patch_edges", "int", 128, "Largest boundary loop (in edges) a planar patch may fill.", "repair", low=3, high=1024),
    _s("repair.max_diagnostic_loops", "int", 256, "Most boundary loops measured and reported in a diagnosis.", "repair", low=1, high=4096),
    _s("repair.max_tolerance_fraction", "float", None, "Cap on the welding tolerance as a fraction of the model diagonal; null uses the repair mode's own ceiling.",
       "repair.policy", nullable=True, low=1e-9, high=0.01),
    _s("repair.enabled_strategies", "strategies", REPAIR_STRATEGIES, "Repair strategies that may be offered; others are reported as disabled.",
       "repair.candidates, repair.capability_info"),

    _s("comparison.default_tolerance", "float", 1e-6, "Relative/absolute comparison tolerance when a request names none.", "app.compare", low=0.0, high=1.0),

    _s("visualization.preview_triangle_limit", "int", 50_000, "Most triangles sent to the 3D viewer (analysis always uses the full mesh).", "engine, app.geometry",
       low=1_000, high=500_000),

    _s("reporting.include_ai_analysis", "bool", True, "Add the AI Analysis chapter to analysis reports when a SLAI agent actually answered.", "engine.report_bytes"),
    _s("reporting.include_repair_evidence", "bool", True, "Add repair diagnosis, candidates and evidence classes to reports.", "engine.report_bytes"),
    _s("reporting.max_preview_bytes", "int", 8 * MiB, "Largest accepted report preview image.", "app.report_preview", low=64 * 1024, high=32 * MiB),
    _s("reporting.filename_prefix", "str", "cadcon-analysis", "Prefix of downloaded analysis report names.", "engine.report_filename"),

    _s("agents.enabled", "bool", True, "Master switch for SLAI agent assistance; deterministic CADCon features never depend on it.", "bridge.agents",
       env="CADCON_AGENTS_ENABLED"),
    _s("agents.default_timeout_seconds", "float", 8.0, "Time budget of one agent call unless the agent has its own timeout_seconds.", "bridge.agents",
       low=0.5, high=120.0),
    _s("agents.load_timeout_seconds", "float", 45.0, "How long a call waits for an agent that is still being loaded for the first time (heavy agents import large libraries).",
       "bridge.agents", low=1.0, high=600.0),
    _s("agents.max_workers", "int", 2, "Agent calls that may run at the same time.", "bridge.agents", low=1, high=8),
    _s("agents.failure_cooldown_seconds", "float", 30.0, "After a timeout or failure an agent is not called again for this long.", "bridge.agents",
       low=0.0, high=3600.0),

    _s("performance.analysis_cache_entries", "int", 32, "Inspection/analysis results cached by source hash and unit (0 disables the cache).", "app.cache",
       low=0, high=512),
)
SETTINGS: Mapping[str, Setting] = MappingProxyType({s.path: s for s in SCHEMA})
CONSUMERS: Mapping[str, str] = MappingProxyType({s.path: s.consumer for s in SCHEMA})
_DYNAMIC_PREFIXES = ("agents.integrations", "agents.workflows")


def _tree_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {"": set()}
    for path in SETTINGS:
        parts = path.split(".")
        for i in range(len(parts)):
            keys.setdefault(".".join(parts[:i]), set()).add(parts[i])
    keys.setdefault("agents", set()).update({"integrations", "workflows"})
    return keys


_KEYS = _tree_keys()


# ---------------------------------------------------------------------------------------------------------- immutable view
def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class CADConConfig:
    """Validated, immutable configuration. ``config.get('repair.mode')`` or ``config.section('repair')`` (read-only mappings)."""

    values: Mapping[str, Any]
    sources: Mapping[str, str]
    path: Path | None
    notes: tuple[str, ...] = ()
    loaded_from: str = "defaults"

    def get(self, dotted: str) -> Any:
        node: Any = self.values
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(f"Unknown CADCon setting: {dotted}")
            node = node[part]
        return node

    def section(self, name: str) -> Mapping[str, Any]:
        node = self.values.get(name)
        if not isinstance(node, Mapping):
            raise KeyError(f"Unknown CADCon configuration section: {name}")
        return node

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def agent_enabled(self, key: str) -> bool:
        """Whether an integrated agent may be called at all (master switch AND its own switch)."""
        if not self.get("agents.enabled"):
            return False
        row = self.get("agents.integrations").get(key)
        return bool(row["enabled"]) if row is not None else False

    def agent_timeout(self, key: str) -> float:
        row = self.get("agents.integrations").get(key) or {}
        specific = row.get("timeout_seconds")
        return float(specific if specific is not None else self.get("agents.default_timeout_seconds"))

    def route(self, workflow: str) -> tuple[str, ...]:
        """Agents configured for ``workflow`` that are currently enabled, in configured order."""
        return tuple(k for k in self.get("agents.workflows").get(workflow, ()) if self.agent_enabled(k))

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.values)

    def diagnostics(self, *, show_path: bool = False) -> dict[str, Any]:
        """What is active and where each value came from. The file path is shown only when asked for (development mode)."""
        counts: dict[str, int] = {}
        for origin in self.sources.values():
            counts[origin] = counts.get(origin, 0) + 1
        return {"schema_version": SCHEMA_VERSION, "loaded_from": self.loaded_from,
                "file": (str(self.path) if show_path else self.path.name) if self.path else None,
                "sources": counts, "overridden": sorted(p for p, o in self.sources.items() if o in ("env", "override")),
                "notes": list(self.notes)}


# --------------------------------------------------------------------------------------------------------------- coercion
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _fail(message: str, **details: Any) -> ConfigurationError:
    return ConfigurationError(message, details=details or None,
                              hint="Fix the setting in cadcon_config.yaml (or remove the override) and restart or reload CADCon.")


def _check_path_text(path: str, text: str, *, absolute: bool) -> str:
    if not text.strip() or any(ord(c) < 32 for c in text):
        raise _fail(f"Invalid {path}: must be a non-empty path without control characters", setting=path)
    parts = Path(text).parts
    if ".." in parts:
        raise _fail(f"Invalid {path}: parent-directory ('..') segments are not allowed", setting=path)
    expanded = os.path.expanduser(text)
    if absolute and not os.path.isabs(expanded):
        raise _fail(f"Invalid {path}: an absolute path is required", setting=path)
    return expanded


def _coerce(setting: Setting, value: Any, origin: str, from_text: bool = False) -> Any:
    path, kind = setting.path, setting.kind
    where = f"{path} ({origin})"
    if value is None:
        if setting.nullable:
            return None
        raise _fail(f"Invalid {where}: a value is required", setting=path)
    if from_text and isinstance(value, str):
        text = value.strip()
        if kind == "bool":
            if text.lower() in _TRUE:
                return True
            if text.lower() in _FALSE:
                return False
            raise _fail(f"Invalid {where}: expected true or false", setting=path)
        if kind == "int":
            try:
                value = int(text)
            except ValueError as exc:
                raise _fail(f"Invalid {where}: expected a whole number", setting=path) from exc
        elif kind == "float":
            try:
                value = float(text)
            except ValueError as exc:
                raise _fail(f"Invalid {where}: expected a number", setting=path) from exc
    if kind == "bool":
        if type(value) is not bool:
            raise _fail(f"Invalid {where}: expected true or false", setting=path)
        return value
    if kind == "int":
        if type(value) is not int:
            raise _fail(f"Invalid {where}: expected a whole number", setting=path)
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _fail(f"Invalid {where}: expected a finite number", setting=path)
        value = float(value)
    if kind in ("int", "float"):
        if (setting.low is not None and value < setting.low) or (setting.high is not None and value > setting.high):
            raise _fail(f"Invalid {where}: must be between {setting.low:g} and {setting.high:g}", setting=path, value=value)
        return value
    if kind in ("str", "choice"):
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise _fail(f"Invalid {where}: expected non-empty text", setting=path)
        value = value.strip()
        if setting.choices and value not in setting.choices:
            raise _fail(f"Invalid {where}: must be one of {', '.join(setting.choices)}", setting=path, value=value)
        if path == "reporting.filename_prefix" and not all(c.isalnum() or c in "-_." for c in value):
            raise _fail(f"Invalid {where}: use letters, digits, '-', '_' or '.' only", setting=path)
        return value
    if kind == "path":
        if not isinstance(value, str):
            raise _fail(f"Invalid {where}: expected a path", setting=path)
        return _check_path_text(path, value, absolute=(path == "conversion.dwg_executable"))
    if kind == "strategies":
        if isinstance(value, str) and from_text:
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple)) or not value or any(not isinstance(v, str) for v in value):
            raise _fail(f"Invalid {where}: expected a non-empty list of strategy names", setting=path)
        unknown = sorted(set(value) - set(REPAIR_STRATEGIES))
        if unknown:
            raise _fail(f"Invalid {where}: unknown repair strategy {', '.join(unknown)}", setting=path,
                        known=list(REPAIR_STRATEGIES))
        if len(set(value)) != len(value):
            raise _fail(f"Invalid {where}: duplicate strategy names", setting=path)
        return tuple(value)
    raise AssertionError(f"unhandled setting kind {kind}")


# ----------------------------------------------------------------------------------------------------------- file reading
def config_file(explicit: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """``(path, explicit)``: the explicit argument, else ``$CADCON_CONFIG``, else the bundled ``configs/cadcon_config.yaml``."""
    environ = os.environ if environ is None else environ
    if explicit:
        return Path(explicit), True
    if environ.get(CONFIG_ENV):
        return Path(environ[CONFIG_ENV]), True
    return DEFAULT_CONFIG_PATH, False


def _read_yaml(path: Path, force_reload: bool) -> dict[str, Any]:
    try:
        from src.utils.configuration import bind_config  # SLAI's shared, side-effect-free repository
    except ImportError as exc:
        raise ConfigurationError("SLAI's shared configuration repository is not available",
                                 hint="Pass --slai-root or set SLAI_ROOT to your SLAI checkout.") from exc
    try:
        data = bind_config(path).load(force_reload=force_reload)
    except FileNotFoundError as exc:
        raise _fail(f"Configuration file not found: {path.name}", file=path.name) from exc
    except (ValueError, TypeError, OSError) as exc:  # YAML errors and DuplicateConfigKeyError are ValueErrors
        raise _fail(f"{path.name} could not be read: {exc}", file=path.name) from exc
    except Exception as exc:
        if type(exc).__module__.split(".")[0] == "yaml":
            raise _fail(f"{path.name} is not valid YAML: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}", file=path.name) from exc
        raise
    data.pop("__config_path__", None)
    return data


def _flatten(raw: Mapping[str, Any], origin: str) -> dict[str, Any]:
    """Validate the *shape* of a raw mapping against the schema tree and return ``{dotted_path: raw value}`` for the static settings."""
    found: dict[str, Any] = {}

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if dotted in _DYNAMIC_PREFIXES:
                found[dotted] = value
                continue
            if dotted in SETTINGS:
                found[dotted] = value
                continue
            if dotted in _KEYS:
                if not isinstance(value, Mapping):
                    raise _fail(f"Invalid {dotted} ({origin}): expected a section", setting=dotted)
                walk(value, dotted)
                continue
            siblings = sorted(_KEYS.get(prefix, ()))
            close = difflib.get_close_matches(str(key), siblings, n=1)
            raise _fail(f"Unknown setting {dotted} ({origin})" + (f"; did you mean '{prefix + '.' if prefix else ''}{close[0]}'?" if close else ""),
                        setting=dotted, known=siblings)

    walk(raw, "")
    return found


def _nested_override(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Accept both ``{'repair.mode': 'x'}`` and ``{'repair': {'mode': 'x'}}`` for overrides."""
    out: dict[str, Any] = {}
    for key, value in overrides.items():
        if "." in str(key) and str(key) not in _DYNAMIC_PREFIXES:
            node = out
            parts = str(key).split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        else:
            out[str(key)] = value
    return out


def _agents_dynamic(raw_integrations: Any, raw_workflows: Any, origin: str) -> tuple[dict[str, Any], dict[str, Any]]:
    integrations = {k: {"enabled": True, "timeout_seconds": None} for k in INTEGRATED_AGENTS}
    if raw_integrations is not None:
        if not isinstance(raw_integrations, Mapping):
            raise _fail(f"Invalid agents.integrations ({origin}): expected a mapping of agent names", setting="agents.integrations")
        for key, row in raw_integrations.items():
            if key not in INTEGRATED_AGENTS:
                close = difflib.get_close_matches(str(key), INTEGRATED_AGENTS, n=1)
                raise _fail(f"Invalid agent identifier '{key}' in agents.integrations ({origin}); CADCon integrates: {', '.join(INTEGRATED_AGENTS)}"
                            + (f"; did you mean '{close[0]}'?" if close else ""), setting="agents.integrations", agent=str(key))
            if not isinstance(row, Mapping):
                raise _fail(f"Invalid agents.integrations.{key} ({origin}): expected a mapping", setting=f"agents.integrations.{key}")
            unknown = sorted(set(row) - {"enabled", "timeout_seconds"})
            if unknown:
                raise _fail(f"Unknown setting agents.integrations.{key}.{unknown[0]} ({origin})", setting=f"agents.integrations.{key}")
            if "enabled" in row:
                integrations[key]["enabled"] = _coerce(Setting(f"agents.integrations.{key}.enabled", "bool", True, ""), row["enabled"], origin)
            if row.get("timeout_seconds") is not None:
                integrations[key]["timeout_seconds"] = _coerce(
                    Setting(f"agents.integrations.{key}.timeout_seconds", "float", 8.0, "", low=0.5, high=120.0), row["timeout_seconds"], origin)
    workflows = {k: tuple(v) for k, v in DEFAULT_WORKFLOW_ROUTES.items()}
    if raw_workflows is not None:
        if not isinstance(raw_workflows, Mapping):
            raise _fail(f"Invalid agents.workflows ({origin}): expected a mapping of workflow names", setting="agents.workflows")
        for name, agents in raw_workflows.items():
            if name not in WORKFLOWS:
                close = difflib.get_close_matches(str(name), list(WORKFLOWS), n=1)
                raise _fail(f"Unknown workflow '{name}' in agents.workflows ({origin}); known: {', '.join(WORKFLOWS)}"
                            + (f"; did you mean '{close[0]}'?" if close else ""), setting="agents.workflows", workflow=str(name))
            if not isinstance(agents, (list, tuple)) or any(not isinstance(a, str) for a in agents) or len(set(agents)) != len(agents):
                raise _fail(f"Invalid agents.workflows.{name} ({origin}): expected a list of distinct agent names", setting=f"agents.workflows.{name}")
            bad = [a for a in agents if a not in WORKFLOWS[name]]
            if bad:
                raise _fail(f"Agent '{bad[0]}' cannot serve workflow '{name}' ({origin}); allowed: {', '.join(WORKFLOWS[name])}",
                            setting=f"agents.workflows.{name}", agent=bad[0])
            workflows[name] = tuple(agents)
    return integrations, workflows


def _unflatten(flat: Mapping[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for dotted, value in flat.items():
        node = tree
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return tree


def _validate_unit(unit: str) -> None:
    try:
        from src.utils.spatial_formats.common.units import require_unit
    except ImportError:
        return
    try:
        require_unit(unit)
    except Exception as exc:
        raise _fail(f"Invalid geometry.default_unit: {exc}", setting="geometry.default_unit") from exc


def validate_config(raw: Mapping[str, Any], *, origin: str = "cadcon_config.yaml") -> dict[str, Any]:
    """Validate a raw (YAML-shaped) mapping and return ``{dotted: value}`` for static settings. Raises :class:`ConfigurationError`."""
    return {p: _coerce(SETTINGS[p], v, origin) for p, v in _flatten(raw, origin).items() if p in SETTINGS}


def load_config(path: str | os.PathLike[str] | None = None, *, overrides: Mapping[str, Any] | None = None,
                environ: Mapping[str, str] | None = None, force_reload: bool = False) -> CADConConfig:
    """Build a validated configuration (does not touch the process-wide active one - see :func:`get_config` / :func:`activate`)."""
    environ = os.environ if environ is None else environ
    file, explicit = config_file(path, environ)
    notes: list[str] = []
    yaml_flat: dict[str, Any] = {}
    raw_integrations = raw_workflows = None
    loaded_from = "defaults"
    if file.is_file():
        raw = _read_yaml(file, force_reload)
        shaped = _flatten(raw, file.name)
        raw_integrations = shaped.pop("agents.integrations", None)
        raw_workflows = shaped.pop("agents.workflows", None)
        yaml_flat = shaped
        loaded_from = "file"
    elif explicit:
        raise _fail(f"Configuration file not found: {file.name}", file=file.name)
    else:
        notes.append(f"No configuration file at {file.name}; the built-in defaults are used.")

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for setting in SCHEMA:
        values[setting.path] = setting.default            # defaults are validated by the test-suite, not at every start
        sources[setting.path] = "default"
    for dotted, value in yaml_flat.items():
        values[dotted] = _coerce(SETTINGS[dotted], value, file.name)
        sources[dotted] = "yaml"
    for setting in SCHEMA:
        name = setting.env
        if name and name in environ and environ[name] != "":
            values[setting.path] = _coerce(setting, environ[name], f"environment {name}", from_text=True)
            sources[setting.path] = "env"
    override_dyn: dict[str, Any] = {}
    if overrides:
        nested = _nested_override(overrides)
        shaped = _flatten(nested, "override")
        override_dyn = {k: shaped.pop(k) for k in list(shaped) if k in _DYNAMIC_PREFIXES}
        for dotted, value in shaped.items():
            if value is None:
                continue                                  # an unset override (e.g. an argparse default) never replaces a value
            values[dotted] = _coerce(SETTINGS[dotted], value, "override")
            sources[dotted] = "override"

    integrations, workflows = _agents_dynamic(
        override_dyn.get("agents.integrations", raw_integrations), override_dyn.get("agents.workflows", raw_workflows),
        "override" if override_dyn else file.name)
    tree = _unflatten(values)
    tree["agents"]["integrations"] = integrations
    tree["agents"]["workflows"] = workflows
    sources["agents.integrations"] = "override" if "agents.integrations" in override_dyn else ("yaml" if raw_integrations is not None else "default")
    sources["agents.workflows"] = "override" if "agents.workflows" in override_dyn else ("yaml" if raw_workflows is not None else "default")

    if values["server.max_request_bytes"] < values["files.max_source_bytes"]:
        raise _fail("server.max_request_bytes must not be smaller than files.max_source_bytes",
                    setting="server.max_request_bytes")
    _validate_unit(values["geometry.default_unit"])
    return CADConConfig(_freeze(tree), MappingProxyType(dict(sources)), file if file.is_file() else None, tuple(notes), loaded_from)


# ------------------------------------------------------------------------------------------------------- active configuration
_LOCK = threading.RLock()
_ACTIVE: CADConConfig | None = None
_ACTIVE_ARGS: dict[str, Any] = {}


def get_config() -> CADConConfig:
    """The process-wide active configuration, loaded once on first use (no repeated disk reads)."""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is None:
            _ACTIVE = load_config(**_ACTIVE_ARGS)
        return _ACTIVE


def get_section(name: str) -> Mapping[str, Any]:
    return get_config().section(name)


def activate(config: CADConConfig, **load_args: Any) -> CADConConfig:
    """Make ``config`` the active one (used by start-up, with the same arguments reused by :func:`reload_config`)."""
    global _ACTIVE, _ACTIVE_ARGS
    with _LOCK:
        _ACTIVE, _ACTIVE_ARGS = config, dict(load_args)
        return config


def reload_config() -> CADConConfig:
    """Re-read the file with the arguments of the last activation. An invalid file raises and the previous configuration stays active."""
    global _ACTIVE
    with _LOCK:
        fresh = load_config(**{**_ACTIVE_ARGS, "force_reload": True})
        _ACTIVE = fresh
        return fresh


def reset_config() -> None:
    """Forget the active configuration (tests, or switching applications in one process)."""
    global _ACTIVE, _ACTIVE_ARGS
    with _LOCK:
        _ACTIVE, _ACTIVE_ARGS = None, {}


__all__ = [
    "SCHEMA", "SETTINGS", "CONSUMERS", "SCHEMA_VERSION", "POLICIES", "REPAIR_MODES", "REPAIR_STRATEGIES", "INTEGRATED_AGENTS", "WORKFLOWS",
    "DEFAULT_WORKFLOW_ROUTES", "DEFAULT_CONFIG_PATH", "CONFIG_ENV", "Setting", "CADConConfig",
    "load_config", "get_config", "get_section", "validate_config", "reload_config", "reset_config", "activate", "config_file",
]
