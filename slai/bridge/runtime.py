"""Lifecycle, routing and isolation for the SLAI agents CADCon talks to.

CADCon never constructs an agent itself: it asks SLAI's ``AgentFactory`` (the one factory) for an instance, calls the agent's documented
``perform_task`` and releases it through the factory. This module adds only what the factory does not know about CADCon:

* configuration (``agents.*`` in cadcon_config.yaml) decides whether an agent may be called at all, for which workflow, and with what budget;
* loading is lazy (first use) and happens once; a call that waits for a first load is bounded by ``agents.load_timeout_seconds``;
* every call has a time budget; a timed-out or failing agent is left alone for ``agents.failure_cooldown_seconds`` so a stuck agent cannot
  pile up threads, and calls are bounded by ``agents.max_workers`` (Python cannot kill a running thread - a timed-out call is *abandoned*,
  counted, and its late result is discarded);
* a failure never propagates: the caller gets an :class:`AgentResult` whose ``status`` says what happened, and nothing is invented.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from ...utils import cadcon_helpers as helpers, config_loader
except ImportError:  # flat layout (run_cadcon.py / tests)
    from utils import cadcon_helpers as helpers, config_loader

#: AgentResult.status values. Only ``completed`` carries an agent's answer.
COMPLETED, DISABLED, UNAVAILABLE, TIMEOUT, FAILED, INVALID, SATURATED = (
    "completed", "disabled", "unavailable", "timeout", "failed", "invalid_response", "saturated")

AGENT_CLASSES = {  # documentation only: the factory resolves the class from its own registry
    "spatial": "src.agents.spatial_agent.SpatialAgent",
    "reasoning": "src.agents.reasoning_agent.ReasoningAgent",
    "verification": "src.agents.verification_agent.VerificationAgent",
}


@dataclass
class AgentResult:
    """Normalised outcome of one agent operation. ``evidence`` records are tagged ``ai_assisted`` only when ``status == completed``."""

    agent: str
    operation: str
    status: str
    result: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    assessment: str | None = None          # the agent's own verdict (e.g. a formal status); never invented here
    confidence: float | None = None        # only when the agent itself supplies one
    limitations: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None
    workflow: str = ""
    operation_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "operation": self.operation, "workflow": self.workflow, "status": self.status,
                "result": self.result, "evidence": self.evidence, "assessment": self.assessment, "confidence_or_assessment": self.assessment
                if self.confidence is None else self.confidence, "limitations": self.limitations, "duration_ms": self.duration_ms,
                "warnings": self.warnings, "reason": self.reason, "operation_id": self.operation_id}


Validator = Callable[[Any], "tuple[Any, str | None, list[str]]"]


class AgentRuntime:
    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._factory_obj: Any = None
        self._factory_cls: Any = None
        self._agents: dict[str, Any] = {}
        self._loading: dict[str, threading.Event] = {}
        self._load_errors: dict[str, str] = {}
        self._loaded_at: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._last_failure: dict[str, str] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._pool_size = 0
        self._inflight = 0
        self._abandoned = 0
        self._stats: dict[str, dict[str, Any]] = {}
        self.calls: deque[dict[str, Any]] = deque(maxlen=200)

    # ---------------------------------------------------------------------------------------------- configuration
    @property
    def config(self) -> Any:
        return self._config if self._config is not None else config_loader.get_config()

    def set_config(self, config: Any) -> None:
        self._config = config

    def enabled(self, key: str) -> bool:
        return self.config.agent_enabled(key)

    # --------------------------------------------------------------------------------------------------- factory
    def _factory(self) -> Any:
        """The SLAI AgentFactory. Re-created only if the factory class itself was replaced (test doubles)."""
        from src.agents import agent_factory as factory_module  # SLAI's single agent factory
        with self._lock:
            if self._factory_obj is None or self._factory_cls is not factory_module.AgentFactory:
                self._factory_obj = factory_module.AgentFactory()
                self._factory_cls = factory_module.AgentFactory
                self._agents.clear()
                self._load_errors.clear()
            return self._factory_obj

    def _load(self, key: str, event: threading.Event) -> None:
        try:
            agent = self._factory().get_agent(key)
            if not callable(getattr(agent, "perform_task", None)):
                raise RuntimeError(f"The {key} agent has no perform_task interface")
            with self._lock:
                self._agents[key] = agent
                self._loaded_at[key] = helpers.now_iso()
                self._load_errors.pop(key, None)
        except BaseException as exc:  # noqa: BLE001 - recorded, never propagated into the request thread
            with self._lock:
                first = (str(exc).splitlines() or [""])[0].split(" component=")[0]
                self._load_errors[key] = f"{type(exc).__name__}: {first[:200]}".rstrip(": ")
        finally:
            with self._lock:
                self._loading.pop(key, None)
            event.set()

    def _sync_factory(self) -> None:
        """Forget cached agents if SLAI's AgentFactory class was replaced (a test double, or a reloaded module)."""
        try:
            from src.agents import agent_factory as factory_module
        except Exception:  # noqa: BLE001 - reported by the load itself
            return
        with self._lock:
            if self._factory_cls is not None and self._factory_cls is not factory_module.AgentFactory:
                self._agents.clear()
                self._load_errors.clear()
                self._factory_obj = self._factory_cls = None

    def _ensure_loaded(self, key: str) -> threading.Event | None:
        """Start (once) a background load; returns the event to wait on, or ``None`` when the agent is already loaded."""
        self._sync_factory()
        with self._lock:
            if key in self._agents:
                return None
            if key in self._loading:
                return self._loading[key]
            event = threading.Event()
            self._loading[key] = event
        threading.Thread(target=self._load, args=(key, event), name=f"cadcon-load-{key}", daemon=True).start()
        return event

    def _executor(self) -> ThreadPoolExecutor:
        size = int(self.config.get("agents.max_workers"))
        with self._lock:
            if self._pool is None or self._pool_size != size:
                if self._pool is not None:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool, self._pool_size = ThreadPoolExecutor(max_workers=size, thread_name_prefix="cadcon-agent"), size
            return self._pool

    # ------------------------------------------------------------------------------------------------------ calls
    def _record(self, result: AgentResult) -> AgentResult:
        if result.status == DISABLED:   # nothing was called, so nothing is counted
            return result
        with self._lock:
            row = self._stats.setdefault(result.agent, {"calls": 0, "completed": 0, "failed": 0, "timeouts": 0, "unavailable": 0, "total_ms": 0.0})
            row["calls"] += 1
            row["total_ms"] += result.duration_ms
            key = {COMPLETED: "completed", TIMEOUT: "timeouts", FAILED: "failed", INVALID: "failed"}.get(result.status, "unavailable")
            row[key] += 1
            self.calls.append({"operation_id": result.operation_id, "agent": result.agent, "workflow": result.workflow,
                               "operation": result.operation, "status": result.status, "duration_ms": result.duration_ms, "at": helpers.now_iso()})
        return result

    def call(self, key: str, workflow: str, operation: str, request: Any, validate: Validator, *, timeout: float | None = None,
             token: helpers.CancellationToken | None = None, evidence_kind: str = helpers.AI_ASSISTED,
             claim: str | None = None) -> AgentResult:
        """Run ``agent.perform_task(request)`` under every configured limit. Never raises for agent-side problems."""
        started = time.perf_counter()
        base = AgentResult(agent=key, operation=operation, status=UNAVAILABLE, workflow=workflow, operation_id=helpers.new_operation_id("agent"))

        def finish(status: str, reason: str | None = None, **fields: Any) -> AgentResult:
            base.status, base.reason = status, reason
            for name, value in fields.items():
                setattr(base, name, value)
            base.duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            return self._record(base)

        cfg = self.config
        if not cfg.get("agents.enabled"):
            return finish(DISABLED, "SLAI agent assistance is switched off (agents.enabled)")
        if not cfg.agent_enabled(key):
            return finish(DISABLED, f"The {key} agent is switched off (agents.integrations.{key}.enabled)")
        if key not in cfg.route(workflow):
            return finish(DISABLED, f"The {key} agent is not routed for the {workflow} workflow (agents.workflows.{workflow})")
        if token is not None and token.cancelled:
            return finish(UNAVAILABLE, "The operation was cancelled before the agent was called")
        with self._lock:
            wait = self._cooldown_until.get(key, 0.0) - time.monotonic()
        if wait > 0:
            return finish(UNAVAILABLE, f"The {key} agent is resting for {wait:.0f} more seconds after a failure or timeout "
                                       f"({self._last_failure.get(key, 'unknown cause')})")
        budget = float(timeout if timeout is not None else cfg.agent_timeout(key))

        event = self._ensure_loaded(key)
        if event is not None and not event.wait(float(cfg.get("agents.load_timeout_seconds"))):
            return finish(UNAVAILABLE, f"The {key} agent is still loading; it will be ready for the next request")
        with self._lock:
            agent = self._agents.get(key)
            error = self._load_errors.get(key)
        if agent is None:
            self._fail(key, error or "load failed")
            return finish(UNAVAILABLE, f"The SLAI {key} agent could not be loaded ({error or 'unknown error'}); deterministic CADCon features are unaffected")

        with self._lock:
            if self._inflight >= int(cfg.get("agents.max_workers")):  # every worker is busy (e.g. with abandoned, still-running calls)
                return finish(SATURATED, "All agent workers are still busy with earlier calls; deterministic CADCon features are unaffected")

        def work() -> Any:
            with self._lock:
                self._inflight += 1
            try:
                return agent.perform_task(request)
            finally:
                self._done()

        future: Future = self._executor().submit(work)
        try:
            response = future.result(timeout=budget)
        except FutureTimeout:
            future.cancel()
            with self._lock:
                self._abandoned += 1
            self._fail(key, f"no answer within {budget:g} s")
            return finish(TIMEOUT, f"The {key} agent did not answer within its {budget:g}-second budget; its late answer is discarded")
        except Exception as exc:  # noqa: BLE001
            self._fail(key, type(exc).__name__)
            return finish(FAILED, f"The {key} agent failed ({type(exc).__name__}); deterministic CADCon features are unaffected")
        try:
            result, assessment, limitations = validate(response)
        except (ValueError, KeyError, TypeError) as exc:
            return finish(INVALID, f"The {key} agent returned a response CADCon cannot use ({exc})")
        evidence = [helpers.evidence(evidence_kind, AGENT_CLASSES.get(key, key), claim or f"{operation} answered by the SLAI {key} agent",
                                     operation_id=base.operation_id)]
        return finish(COMPLETED, None, result=result, assessment=assessment, limitations=limitations, evidence=evidence)

    def _done(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def _fail(self, key: str, cause: str) -> None:
        with self._lock:
            self._last_failure[key] = cause
            self._cooldown_until[key] = time.monotonic() + float(self.config.get("agents.failure_cooldown_seconds"))

    # --------------------------------------------------------------------------------------------- health / status
    def health(self, key: str, *, probe: bool = False) -> dict[str, Any]:
        """State of one integrated agent. ``probe=True`` loads it (lazy initialisation) and reads its own capability report."""
        enabled = self.enabled(key)
        row: dict[str, Any] = {"key": key, "enabled": enabled, "class": AGENT_CLASSES.get(key)}
        if not enabled:
            row.update(state="disabled", reason="Switched off in cadcon_config.yaml")
            return row
        if probe:
            event = self._ensure_loaded(key)
            if event is not None:
                event.wait(float(self.config.get("agents.load_timeout_seconds")))
        with self._lock:
            agent, error, loading = self._agents.get(key), self._load_errors.get(key), key in self._loading
            wait = self._cooldown_until.get(key, 0.0) - time.monotonic()
            loaded_at = self._loaded_at.get(key)
            stats = dict(self._stats.get(key, {}))
        if agent is not None:
            row.update(state="ready", loaded_at=loaded_at)
            capabilities = getattr(agent, "capabilities", None)
            if callable(capabilities):
                try:
                    caps = capabilities()
                    row["operations"] = list(caps.get("operations", ())) if isinstance(caps, dict) else None
                except Exception as exc:  # noqa: BLE001
                    row["warnings"] = [f"capabilities() failed: {type(exc).__name__}"]
        elif loading:
            row.update(state="loading")
        elif error:
            row.update(state="unavailable", reason=error)
        else:
            row.update(state="not_loaded", reason="Agents are loaded on first use")
        if wait > 0:
            row.update(cooldown_seconds=round(wait, 1), last_failure=self._last_failure.get(key))
        if stats:
            calls = max(stats["calls"], 1)
            row["metrics"] = {**{k: v for k, v in stats.items() if k != "total_ms"}, "mean_ms": round(stats["total_ms"] / calls, 2)}
        return row

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {"inflight": self._inflight, "abandoned_calls": self._abandoned, "recent_calls": list(self.calls)[-20:],
                    "per_agent": {k: dict(v) for k, v in self._stats.items()}}

    # ----------------------------------------------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Release every agent through the factory, stop the pool and forget cached state. Safe to call repeatedly."""
        with self._lock:
            factory, keys = self._factory_obj, list(self._agents)
            pool = self._pool
            self._agents.clear()
            self._pool = None
            self._factory_obj = self._factory_cls = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if factory is not None:
            for key in keys:
                try:
                    factory.release(key)
                except Exception:  # noqa: BLE001 - shutting down; nothing useful to do
                    pass
            try:
                factory.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def reset_cooldown(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._cooldown_until.clear()
            else:
                self._cooldown_until.pop(key, None)


__all__ = ["AgentRuntime", "AgentResult", "COMPLETED", "DISABLED", "UNAVAILABLE", "TIMEOUT", "FAILED", "INVALID", "SATURATED", "AGENT_CLASSES"]
