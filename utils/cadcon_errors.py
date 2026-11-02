"""CADCon's error classification and translation layer.

CADCon does not define a second error framework. Every class here extends the shared hierarchy in ``applications/tools/errors.py`` (via
the existing HTTP statuses), so the SLAI server turns each one into the same ``{"ok": false, "error": {code, message, hint, details}}``
envelope as every other SLAI application. What CADCon adds is only what is specific to CAD processing:

* a stable ``code`` per condition the interface can tell apart,
* ``category`` (input / geometry / dependency / agent / configuration / resource / internal) and ``recoverable`` (can the person fix
  this and retry?), and
* :func:`classify_exception`, which maps the exceptions the geometry engine and SLAI adapters raise onto these classes **without ever
  hiding a programming error** - an exception that is not an expected input or capability condition is returned as ``None`` so the caller
  re-raises it unchanged.

Messages written for people keep their original wording so existing callers and tests that match on text keep working. ``to_dict`` removes
local filesystem paths from messages, hints and details before they leave the process.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from applications.tools.errors import (AppError, BadRequest, DependencyMissing, LimitExceeded)

_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/](?:[^\s'\"<>|:*?\\/]+[\\/])*([^\s'\"<>|:*?\\/]*)")
_POSIX_PATH = re.compile(r"(?<![\w:/.-])/(?:[\w.@+ -]+/)+([\w.@+-]*)")
_SENSITIVE_KEYS = ("token", "secret", "password", "authorization", "cookie", "api_key")


def scrub_text(value: Any) -> Any:
    """Replace absolute filesystem paths in ``value`` by ``<path>/<file name>`` so error output never discloses local folders."""
    if not isinstance(value, str):
        return value
    cleaned = _WINDOWS_PATH.sub(lambda m: f"<path>/{m.group(1)}" if m.group(1) else "<path>", value)
    return _POSIX_PATH.sub(lambda m: f"<path>/{m.group(1)}" if m.group(1) else "<path>", cleaned)


def scrub_details(value: Any, _depth: int = 0) -> Any:
    """A copy of ``value`` with paths removed from strings and secret-looking entries dropped. Bounded depth; never raises."""
    if _depth > 6:
        return "<truncated>"
    if isinstance(value, Mapping):
        return {str(k): scrub_details(v, _depth + 1) for k, v in value.items()
                if not any(marker in str(k).lower() for marker in _SENSITIVE_KEYS)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub_details(v, _depth + 1) for v in list(value)[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub_text(str(value))[:600]


class CADConError(AppError, RuntimeError):
    """Base of every CADCon-specific condition. Also a ``RuntimeError`` for callers written before the shared hierarchy was adopted.

    The historical forms ``CADConError()``, ``CADConErrorWithMessage(message)`` and ``CADConErrorWithCode(code)`` remain valid.
    """

    status, code = 500, "CADCon.ERROR"
    category = "internal"
    recoverable = False

    def __init__(self, message: str = "CADCon could not complete the operation", *, details: Mapping[str, Any] | None = None,
                 hint: str | None = None, code: str | None = None, category: str | None = None,
                 recoverable: bool | None = None) -> None:
        AppError.__init__(self, message, details=dict(details) if details else None, hint=hint)
        if code is not None:
            self.code = code
        if category is not None:
            self.category = category
        if recoverable is not None:
            self.recoverable = recoverable

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": scrub_text(self.message),
                               "category": self.category, "recoverable": bool(self.recoverable)}
        if self.hint:
            out["hint"] = scrub_text(self.hint)
        if self.details:
            out["details"] = scrub_details(self.details)
        return out


class CADConErrorWithMessage(CADConError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.args = (message,)


class CADConErrorWithCode(CADConError):
    def __init__(self, code: str) -> None:
        super().__init__(code, code=code)
        self.args = (code,)


# ------------------------------------------------------------------------------------------------------------------ input
class InvalidSourceError(CADConError, BadRequest):
    """The CAD source is unreadable, empty, truncated, or breaks its format's rules."""

    status, code, category, recoverable = 400, "cadcon_invalid_source", "input", True


class UnsupportedFormatError(CADConError, BadRequest):
    status, code, category, recoverable = 400, "cadcon_unsupported_format", "input", True


class UnsupportedConversionError(CADConError, BadRequest):
    """No verified route exists for this source/target pair or policy. Nothing was written."""

    status, code, category, recoverable = 400, "cadcon_unsupported_conversion", "input", True


class InvalidUnitError(CADConError, BadRequest):
    status, code, category, recoverable = 400, "cadcon_invalid_unit", "input", True


class ToleranceError(CADConError, BadRequest):
    """A welding or comparison tolerance is not usable (not finite, negative, or larger than the model allows)."""

    status, code, category, recoverable = 400, "cadcon_tolerance_invalid", "input", True


# -------------------------------------------------------------------------------------------------------------- geometry
class GeometryDefectError(CADConError, BadRequest):
    """The geometry is corrupted, topologically invalid or non-manifold in a way the requested operation cannot accept."""

    status, code, category, recoverable = 400, "cadcon_geometry_invalid", "geometry", True


class RepairError(CADConError):
    """A repair strategy or mesh reconstruction could not produce a result that passes independent validation."""

    status, code, category, recoverable = 422, "cadcon_repair_failed", "geometry", True


class FidelityError(CADConError):
    """An output was written but re-reading it did not reproduce the verified properties (or it could not be re-read)."""

    status, code, category, recoverable = 422, "cadcon_fidelity_violation", "geometry", False


class ReportError(CADConError, BadRequest):
    status, code, category, recoverable = 400, "cadcon_report_invalid", "input", True


# ------------------------------------------------------------------------------------------------------------ dependency
class MissingBackendError(CADConError, DependencyMissing):
    """An optional conversion backend (ezdxf, IfcOpenShell, ODA File Converter, …) is not installed or configured."""

    status, code, category, recoverable = 503, "cadcon_backend_missing", "dependency", True


class ConfigurationError(CADConError):
    """``cadcon_config.yaml``, an override, or an environment setting is invalid. The message names the setting."""

    status, code, category, recoverable = 500, "cadcon_config_invalid", "configuration", True


class ResourceLimitError(CADConError, LimitExceeded):
    status, code, category, recoverable = 413, "cadcon_resource_limit", "resource", True


class OperationCancelled(CADConError):
    """The operation was cancelled before it finished; no result was registered and partial output was removed."""

    status, code, category, recoverable = 409, "cadcon_operation_cancelled", "resource", True


# ---------------------------------------------------------------------------------------------------------------- agents
class AgentError(CADConError):
    """Base of agent-integration conditions. These never stop deterministic CADCon operations unless the caller required the agent."""

    status, code, category, recoverable = 503, "cadcon_agent_error", "agent", True


class AgentUnavailable(AgentError, DependencyMissing):
    status, code = 503, "cadcon_agent_unavailable"


class AgentInitializationError(AgentError):
    status, code = 503, "cadcon_agent_init_failed"


class AgentCapabilityUnavailable(AgentError):
    status, code = 501, "cadcon_agent_capability_unavailable"


class AgentTimeout(AgentError):
    status, code = 504, "cadcon_agent_timeout"


class AgentResponseInvalid(AgentError):
    status, code = 502, "cadcon_agent_response_invalid"


# -------------------------------------------------------------------------------------------------------- classification
_TOLERANCE_WORDS = ("welding tolerance", "comparison tolerance", "tolerance must", "reduce the tolerance")
_UNIT_WORDS = ("unit",)
_CONVERSION_WORDS = ("no verified conversion", "no verified converter", "unsupported 3d output", "unsupported 2d output",
                     "select a different target", "cannot be preserved", "strict exchange", "under the selected policy",
                     "unknown conversion policy", "not preserved by the cadcon")
_FIDELITY_WORDS = ("round-trip", "did not pass content verification", "changed verified topology")
_EXTENSION_WORDS = ("unrecognized cad file extension", "no verified geometry reader", "inspection is not implemented")
_DEFECT_WORDS = ("non-finite", "invalid vertex indices", "nonexistent vertex", "triangle", "complexity limit", "non-manifold")


def classify_message(message: str) -> type[CADConError]:
    """The most specific class for a geometry-engine message. Used only for messages the engine itself wrote."""
    text = message.lower()
    if any(word in text for word in _TOLERANCE_WORDS):
        return ToleranceError
    if any(word in text for word in _FIDELITY_WORDS):
        return FidelityError
    if any(word in text for word in _CONVERSION_WORDS):
        return UnsupportedConversionError
    if any(word in text for word in _EXTENSION_WORDS):
        return UnsupportedFormatError
    if "requires a configured" in text or "backend" in text and ("unavailable" in text or "missing" in text):
        return MissingBackendError
    if any(word in text for word in _DEFECT_WORDS):
        return GeometryDefectError
    return InvalidSourceError


def classify_exception(exc: BaseException) -> CADConError | None:
    """Translate an *expected* engine / adapter exception into a CADCon error, or return ``None`` for anything else.

    ``None`` means "this is not a known input or capability condition": the caller must re-raise ``exc`` unchanged so that real defects
    stay visible. Already-classified :class:`AppError` instances are returned untouched.
    """
    if isinstance(exc, AppError):
        return exc if isinstance(exc, CADConError) else None
    name = type(exc).__name__
    if name == "GeometryError" or name == "SpatialFormatError":
        return classify_message(str(exc))(str(exc))
    if name == "SLDCError":
        return ReportError(f"The native SLDC report is invalid: {exc}")
    if isinstance(exc, FileExistsError):
        return InvalidSourceError("An output file with that name already exists", code="cadcon_output_exists",
                                  hint="Choose a different output name; CADCon never overwrites files.")
    if isinstance(exc, ImportError):
        return MissingBackendError(f"An optional CAD backend is unavailable: {exc}",
                                   hint="Install the optional dependency named in the message, or choose a route without it.")
    if isinstance(exc, ValueError):
        return classify_message(str(exc))(str(exc))
    return None


__all__ = [
    "CADConError", "CADConErrorWithMessage", "CADConErrorWithCode",
    "InvalidSourceError", "UnsupportedFormatError", "UnsupportedConversionError", "InvalidUnitError", "ToleranceError",
    "GeometryDefectError", "RepairError", "FidelityError", "ReportError",
    "MissingBackendError", "ConfigurationError", "ResourceLimitError", "OperationCancelled",
    "AgentError", "AgentUnavailable", "AgentInitializationError", "AgentCapabilityUnavailable", "AgentTimeout", "AgentResponseInvalid",
    "classify_exception", "classify_message", "scrub_text", "scrub_details",
]
