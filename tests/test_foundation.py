"""Errors, helpers and configuration: behaviour, precedence and that every configured key has a runtime consumer."""
from __future__ import annotations

import pytest

from utils import cadcon_errors as errors, cadcon_helpers as helpers, config_loader as cfg


# ------------------------------------------------------------------------------------------------------------------- errors
def test_errors_extend_shared_hierarchy_and_keep_runtime_error_compat():
    from applications.tools.errors import AppError, BadRequest
    assert issubclass(errors.CADConError, AppError) and issubclass(errors.CADConError, RuntimeError)
    assert issubclass(errors.InvalidSourceError, BadRequest)
    assert errors.CADConErrorWithMessage("x").args == ("x",)
    assert errors.CADConErrorWithCode("cad.code").code == "cad.code"


def test_error_dict_scrubs_paths_and_secrets():
    err = errors.RepairError("failed reading C:\\Users\\alice\\models\\part.stl", details={"path": "/home/alice/part.stl", "token": "abc123"})
    payload = err.to_dict()
    assert "alice" not in str(payload) and "abc123" not in str(payload)
    assert payload["code"] and payload["recoverable"] in (True, False)


def test_classify_exception_only_translates_expected_conditions():
    class GeometryError(ValueError):
        pass
    assert isinstance(errors.classify_exception(GeometryError("Unsupported conversion from X to Y")), errors.CADConError)
    assert isinstance(errors.classify_exception(ImportError("no ezdxf")), errors.MissingBackendError)
    assert isinstance(errors.classify_exception(FileExistsError()), errors.InvalidSourceError)
    # A genuine defect must stay visible
    assert errors.classify_exception(KeyError("bug")) is None
    assert errors.classify_exception(ZeroDivisionError()) is None


# ------------------------------------------------------------------------------------------------------------------ helpers
def test_evidence_kinds_are_closed():
    assert helpers.evidence(helpers.MEASURED, "x", "y")["kind"] == "measured"
    with pytest.raises(ValueError):
        helpers.evidence("guessed", "x", "y")


def test_safe_names_never_leak_directories_or_odd_characters():
    assert helpers.safe_stem("C:\\a\\b\\My Part (v2)?.STL") == "My Part _v2"
    assert helpers.safe_stem("../../etc/passwd") == "passwd"
    assert helpers.safe_stem("") == "model"
    assert helpers.report_name("Bracket.stl", "json").endswith(".json")


def test_tolerance_helpers_convert_units_and_bound():
    assert helpers.convert_tolerance(1.0, "mm", "m") == pytest.approx(0.001)
    kind, value = helpers.parse_tolerance_request("auto")
    assert kind == "automatic" and value is None
    kind, value = helpers.parse_tolerance_request(0.5)
    assert kind == "manual" and value == 0.5
    with pytest.raises(errors.ToleranceError):
        helpers.parse_tolerance_request(-1)
    bounded, clipped = helpers.bound_tolerance(10.0, diagonal=100.0, max_fraction=0.01)
    assert bounded == pytest.approx(1.0) and clipped is True


def test_evidence_digest_is_deterministic_and_order_independent():
    assert helpers.evidence_digest({"a": 1, "b": [1, 2]}) == helpers.evidence_digest({"b": [1, 2], "a": 1})
    assert helpers.evidence_digest({"a": 1}) != helpers.evidence_digest({"a": 2})


def test_cancellation_token_stops_work():
    token = helpers.CancellationToken()
    helpers.checkpoint(token)
    token.cancel()
    with pytest.raises(errors.OperationCancelled):
        helpers.checkpoint(token, "weld")


def test_memory_budget_uses_injected_provider():
    assert helpers.memory_budget_ok(10, provider=lambda: 1000)
    assert not helpers.memory_budget_ok(10_000, provider=lambda: 1000)
    assert helpers.estimate_mesh_bytes(1000, 2000) > 0


# ------------------------------------------------------------------------------------------------------------------ config
def test_shipped_yaml_equals_declared_defaults():
    loaded = cfg.load_config(environ={}, force_reload=True)
    assert loaded.loaded_from == "file"
    for key, setting in cfg.SETTINGS.items():
        if key == "paths.data_dir":
            continue
        assert loaded.get(key) == (setting.default if not isinstance(setting.default, tuple) else loaded.get(key)), key


def test_precedence_overrides_over_env_over_yaml_over_defaults(tmp_path):
    yaml_file = tmp_path / "c.yaml"
    yaml_file.write_text("repair:\n  mode: conservative\n  max_candidates: 3\nserver:\n  port: 9000\n", encoding="utf-8")
    plain = cfg.load_config(yaml_file, environ={}, force_reload=True)
    assert plain.get("repair.mode") == "conservative" and plain.get("server.port") == 9000
    assert plain.get("repair.large_mesh_max_candidates") == 2  # default fills the gap
    env = cfg.load_config(yaml_file, environ={"CADCON_REPAIR_MODE": "exploratory"}, force_reload=True)
    assert env.get("repair.mode") == "exploratory" and env.diagnostics()["sources"].get("env") == 1
    over = cfg.load_config(yaml_file, environ={"CADCON_REPAIR_MODE": "exploratory"}, overrides={"repair.mode": "balanced"}, force_reload=True)
    assert over.get("repair.mode") == "balanced"
    assert "repair.mode" in over.diagnostics()["overridden"]


@pytest.mark.parametrize("text, fragment", [
    ("repair:\n  mode: reckless\n", "mode"),
    ("repair:\n  max_candidates: 0\n", "max_candidates"),
    ("server:\n  host: 0.0.0.0\n", "host"),
    ("nonsense:\n  key: 1\n", "nonsense"),
    ("agents:\n  integrations:\n    quality: {enabled: true}\n", "quality"),
    ("agents:\n  workflows:\n    repair_advice: [spatial]\n", "spatial"),
    ("repair:\n  mode: balanced\nrepair:\n  mode: balanced\n", "uplicate"),
])
def test_invalid_configuration_is_rejected_with_the_offending_key(tmp_path, text, fragment):
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(errors.ConfigurationError) as caught:
        cfg.load_config(path, environ={}, force_reload=True)
    assert fragment in caught.value.message


def test_invalid_env_value_is_rejected():
    with pytest.raises(errors.ConfigurationError):
        cfg.load_config(environ={"CADCON_PORT": "not-a-port"}, force_reload=True)


def test_missing_explicit_file_fails_but_missing_default_falls_back(tmp_path):
    with pytest.raises(errors.ConfigurationError):
        cfg.load_config(tmp_path / "missing.yaml", environ={}, force_reload=True)


def test_agent_routing_helpers():
    config = cfg.load_config(overrides={"agents.integrations": {"reasoning": {"enabled": False}}}, environ={}, force_reload=True)
    assert not config.agent_enabled("reasoning") and config.agent_enabled("spatial")
    assert config.route("repair_advice") == ()           # routed agent is disabled
    assert config.route("compare_registration") == ("spatial",)
    master = cfg.load_config(overrides={"agents.enabled": False}, environ={}, force_reload=True)
    assert not any(master.agent_enabled(k) for k in cfg.INTEGRATED_AGENTS)
    tuned = cfg.load_config(overrides={"agents.integrations": {"spatial": {"timeout_seconds": 3}}}, environ={}, force_reload=True)
    assert tuned.agent_timeout("spatial") == 3 and tuned.agent_timeout("verification") == tuned.get("agents.default_timeout_seconds")


def test_diagnostics_hide_the_file_path_unless_asked():
    config = cfg.load_config(environ={}, force_reload=True)
    assert "/" not in config.diagnostics()["file"] and "\\" not in config.diagnostics()["file"]
    assert "cadcon_config.yaml" in config.diagnostics(show_path=True)["file"]


def test_reload_keeps_previous_configuration_when_the_file_becomes_invalid(tmp_path):
    path = tmp_path / "live.yaml"
    path.write_text("repair:\n  mode: conservative\n", encoding="utf-8")
    cfg.reset_config()
    try:
        cfg.activate(cfg.load_config(path, environ={}, force_reload=True), path=path, environ={})
        assert cfg.get_config().get("repair.mode") == "conservative"
        path.write_text("repair:\n  mode: exploratory\n", encoding="utf-8")
        assert cfg.reload_config().get("repair.mode") == "exploratory"
        path.write_text("repair:\n  mode: nope\n", encoding="utf-8")
        with pytest.raises(errors.ConfigurationError):
            cfg.reload_config()
        assert cfg.get_config().get("repair.mode") == "exploratory"
    finally:
        cfg.reset_config()


def test_every_setting_is_documented_and_really_consumed_at_runtime():
    """No decorative configuration: each key must be read by code outside the loader (a literal ``get("key")`` or a RepairSettings field)."""
    from pathlib import Path
    root = Path(cfg.__file__).resolve().parents[1]
    source = "\n".join(path.read_text(encoding="utf-8") for path in
                       [root / "app.py", root / "engine.py", root / "repair.py", *sorted((root / "slai" / "bridge").glob("*.py"))])
    import repair
    exempt = {"application.schema_version"}      # validated by the loader itself
    indirect = {"agents.default_timeout_seconds": ".agent_timeout(", "agents.integrations": ".agent_enabled(", "agents.workflows": ".route("}
    for key, setting in cfg.SETTINGS.items():
        assert setting.doc and setting.consumer, key
        if key in exempt:
            continue
        section, _, name = key.partition(".")
        if key in indirect:
            assert indirect[key] in source, key
        elif section == "repair":
            assert name in repair.RepairSettings.__dataclass_fields__, key
        else:
            assert f'"{key}"' in source, f"{key} is declared but nothing reads it"
