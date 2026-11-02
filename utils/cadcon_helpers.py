"""Small, typed helpers that are specific to CADCon and shared by ``engine``, ``repair``, ``app`` and the agent bridge.

Nothing here parses CAD files, builds geometry or owns a registry that SLAI already provides:

* length units come from SLAI's unit registry (``src.utils.spatial_formats.common.units``) - this module never defines its own table;
* timestamps use ``applications.tools.validation.utc_now``; logging stays with SLAI's logger (the modules that log import it themselves);
* the five evidence classes (:data:`EVIDENCE_KINDS`) keep *measured*, *verified*, *AI-assisted*, *estimated* and *unavailable* results apart.

Importing this module has no side effects. Everything is deterministic unless stated.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from applications.tools.validation import utc_now

from .cadcon_errors import InvalidUnitError, OperationCancelled, ToleranceError

# ------------------------------------------------------------------------------------------------------------------ evidence
MEASURED, VERIFIED, AI_ASSISTED, ESTIMATED, UNAVAILABLE = "measured", "verified", "ai_assisted", "estimated", "unavailable"
EVIDENCE_KINDS = (MEASURED, VERIFIED, AI_ASSISTED, ESTIMATED, UNAVAILABLE)


def evidence(kind: str, source: str, claim: str, **fields: Any) -> dict[str, Any]:
    """One evidence record. ``kind`` is never interchangeable: *measured* comes from deterministic geometry code, *verified* names a check
    that actually ran, *ai_assisted* names a SLAI agent that actually answered, *estimated* names its approximation, and *unavailable*
    means the capability was not run (``fields['reason']`` says why)."""
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"Unknown evidence kind {kind!r}; expected one of {', '.join(EVIDENCE_KINDS)}")
    return {"kind": kind, "source": source, "claim": claim, **fields}


def warning_record(code: str, message: str, **context: Any) -> dict[str, Any]:
    """A structured processing warning (stable ``code`` for the interface, ``message`` for people)."""
    record: dict[str, Any] = {"code": code, "message": message}
    if context:
        record["context"] = context
    return record


# ------------------------------------------------------------------------------------------------------------------ identity
def now_iso() -> str:
    return utc_now()


def new_operation_id(prefix: str = "op") -> str:
    """A short opaque id for one operation (``op-3f9a…``), used to correlate agent calls, provenance and log lines."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    """Stable JSON text (sorted keys, no NaN) for hashing and reproducible reports."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)


def evidence_digest(value: Any) -> str:
    """Deterministic fingerprint of an evidence package: equal evidence always gives an equal digest."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------------------ file naming
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_EXTENSION = re.compile(r"^\.?[A-Za-z0-9]{1,8}$")


def safe_stem(name: str, fallback: str = "model", limit: int = 96) -> str:
    """The file name without directory or extension, reduced to ASCII letters, digits and ``._ -`` (browser- and Windows-safe)."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(base).stem
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    cleaned = _SAFE_CHARS.sub("_", ascii_stem).strip(" ._")[:limit].rstrip(" ._")
    return cleaned or fallback


def normalize_extension(value: str) -> str:
    """``'STL'`` / ``'.stl'`` → ``'.stl'``. Raises ``ValueError`` for anything that is not a short alphanumeric extension."""
    text = str(value or "").strip()
    if not _EXTENSION.match(text):
        raise ValueError("Choose a supported output format")
    return "." + text.lstrip(".").lower()


def output_name(source_name: str, extension: str, *, suffix: str | None = None) -> str:
    """``part 1.obj`` + ``.sgeo`` → ``part 1.sgeo`` (or ``part 1-repaired.sgeo`` with ``suffix='repaired'``)."""
    tail = f"-{safe_stem(suffix, 'out')}" if suffix else ""
    return f"{safe_stem(source_name)}{tail}{normalize_extension(extension)}"


def report_name(source_name: str | None, fmt: str, *, prefix: str = "cadcon-analysis") -> str:
    return f"{prefix}-{safe_stem(source_name or 'model')}.{normalize_extension(fmt).lstrip('.')}"


def file_metadata(path: Path | str, *, original_name: str | None = None) -> dict[str, Any]:
    """Size, extension and SHA-256 of a regular file. The original upload name is reported, never the stored path."""
    target = Path(path)
    return {"name": original_name or target.name, "format": target.suffix.lower().lstrip("."),
            "size_bytes": target.stat().st_size, "sha256": sha256_file(target)}


def format_bytes(value: int | float | None) -> str:
    if value is None or isinstance(value, bool) or value != value or value < 0:
        return "–"
    if value < 1024:
        return f"{int(value)} B"
    size = float(value) / 1024.0
    unit = "KB"
    for unit in ("KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            break
        size /= 1024.0
    return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"


# ------------------------------------------------------------------------------------------------------------------ numbers
def is_finite_vector(value: Any, length: int = 3) -> bool:
    return (isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == length
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in value))


def require_finite(value: Any, name: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToleranceError(f"{name} must be a finite number")
    return float(value)


def bounds_extents(bounds: Mapping[str, Sequence[float]] | None) -> list[float] | None:
    """Axis lengths of an ``{"min": [...], "max": [...]}`` box, or ``None`` when the box is missing or not finite."""
    if not bounds or not is_finite_vector(bounds.get("min")) or not is_finite_vector(bounds.get("max")):
        return None
    return [float(hi) - float(lo) for lo, hi in zip(bounds["min"], bounds["max"])]


def bounds_diagonal(bounds: Mapping[str, Sequence[float]] | None) -> float:
    """Length of the bounding-box diagonal (the model's scale); ``0.0`` if it cannot be determined."""
    extents = bounds_extents(bounds)
    return math.sqrt(sum(e * e for e in extents)) if extents else 0.0


def vertex_bounds(vertices: Iterable[Sequence[float]]) -> dict[str, list[float]] | None:
    low = [math.inf] * 3
    high = [-math.inf] * 3
    seen = False
    for vertex in vertices:
        seen = True
        for axis in range(3):
            low[axis] = min(low[axis], vertex[axis])
            high[axis] = max(high[axis], vertex[axis])
    return {"min": low, "max": high} if seen else None


def stable_float(value: float | None, digits: int = 12) -> float | None:
    """Round to ``digits`` significant digits so equal measurements serialize identically."""
    if value is None or not math.isfinite(value) or value == 0:
        return value
    return float(f"{value:.{digits}g}")


# ------------------------------------------------------------------------------------------------------ units & tolerances
def length_unit(unit: str) -> str:
    """Validate a declared length unit through SLAI's registry (the one authoritative table)."""
    from src.utils.spatial_formats.common.units import require_unit
    try:
        return require_unit(unit)
    except Exception as exc:  # the registry raises its own SpatialFormatError
        raise InvalidUnitError(str(exc), hint="Use a unit from the SLAI registry such as mm, cm, m, in or ft.") from exc


def convert_tolerance(value: float, source_unit: str | None, target_unit: str | None) -> float:
    """Express a length tolerance in another unit. Without both units the number is returned unchanged (and callers must say so)."""
    if not source_unit or not target_unit or source_unit == target_unit:
        return float(value)
    from src.utils.spatial_formats.common.units import convert_length
    return float(convert_length(float(value), source_unit, target_unit))


def tolerance_fraction(tolerance: float, diagonal: float) -> float | None:
    return tolerance / diagonal if diagonal > 0 and math.isfinite(tolerance) else None


def format_tolerance(tolerance: float, unit: str | None, *, diagonal: float | None = None, display_unit: str | None = None) -> str:
    """``0.0012 mm (0.00040% of the model diagonal)``. ``display_unit`` re-expresses the value through the SLAI registry."""
    shown, label = tolerance, unit
    if display_unit and unit and display_unit != unit:
        shown, label = convert_tolerance(tolerance, unit, display_unit), display_unit
    text = f"{shown:.6g} {label or 'source units'}"
    if diagonal and diagonal > 0:
        text += f" ({100.0 * tolerance / diagonal:.3g}% of the model diagonal)"
    return text


def parse_tolerance_request(requested: Any) -> tuple[str, float | None]:
    """``None`` / ``''`` / ``'auto'`` → ``('automatic', None)``; a finite non-negative number → ``('manual', value)``."""
    if requested is None or requested == "" or requested == "auto":
        return "automatic", None
    if type(requested) not in (int, float):
        raise ToleranceError("Welding tolerance must be a number or 'auto'")
    value = float(requested)
    if not (0 <= value < math.inf):
        raise ToleranceError("Welding tolerance must be finite and non-negative")
    return "manual", value


def bound_tolerance(value: float, diagonal: float, max_fraction: float) -> tuple[float, bool]:
    """Clamp a recommended tolerance to ``max_fraction`` of the model diagonal. Returns ``(value, was_clamped)``."""
    ceiling = diagonal * max_fraction
    return (ceiling, True) if diagonal > 0 and value > ceiling else (value, False)


# ---------------------------------------------------------------------------------------------- timing & resources
class Stopwatch:
    """Monotonic timer: ``with Stopwatch() as t: ...; t.ms``."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._end: float | None = None

    def __enter__(self) -> "Stopwatch":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: Any) -> None:
        self._end = time.perf_counter()

    @property
    def ms(self) -> float:
        return round(((self._end if self._end is not None else time.perf_counter()) - self._start) * 1000.0, 3)


class CancellationToken:
    """Cooperative cancellation: long loops call :func:`checkpoint`, which raises :class:`OperationCancelled` once :meth:`cancel` ran."""

    def __init__(self, deadline_seconds: float | None = None) -> None:
        self._event = threading.Event()
        self._deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (self._deadline is not None and time.monotonic() > self._deadline)


def checkpoint(token: CancellationToken | None, what: str = "operation") -> None:
    if token is not None and token.cancelled:
        raise OperationCancelled(f"The {what} was cancelled before it finished; no result was registered")


def estimate_mesh_bytes(vertices: int, faces: int) -> int:
    """Conservative working-set estimate for a Python list-of-lists mesh (what CADCon holds while repairing)."""
    return int(vertices) * 120 + int(faces) * 110


def memory_budget_ok(required_bytes: int, *, max_fraction: float = 0.5, provider: Callable[[], int] | None = None) -> bool:
    """Whether ``required_bytes`` fits within ``max_fraction`` of the memory available now. Unknown availability counts as *fits*."""
    try:
        if provider is not None:
            available = provider()
        else:
            import psutil
            available = int(psutil.virtual_memory().available)
    except Exception:
        return True
    return required_bytes <= available * max_fraction


__all__ = [
    "MEASURED", "VERIFIED", "AI_ASSISTED", "ESTIMATED", "UNAVAILABLE", "EVIDENCE_KINDS", "evidence", "warning_record",
    "now_iso", "new_operation_id", "sha256_bytes", "sha256_file", "canonical_json", "evidence_digest",
    "safe_stem", "normalize_extension", "output_name", "report_name", "file_metadata", "format_bytes",
    "is_finite_vector", "require_finite", "bounds_extents", "bounds_diagonal", "vertex_bounds", "stable_float",
    "length_unit", "convert_tolerance", "tolerance_fraction", "format_tolerance", "parse_tolerance_request", "bound_tolerance",
    "Stopwatch", "CancellationToken", "checkpoint", "estimate_mesh_bytes", "memory_budget_ok",
]
