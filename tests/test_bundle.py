"""The installer's bundle builder (tools/desktop/build_bundle.py) and the start-up check it relies on (utils/desktop_support.py). Everything that does not
need Windows, the network or the embeddable Python: matching, staging, the import-closure safety net, the manifest and its verification, the pip command line,
and the environment report."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from utils import desktop_support as support

APP = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cadcon_build_bundle", APP / "tools" / "desktop" / "build_bundle.py")
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------------------------------------------------------------------- matching
@pytest.mark.parametrize("pattern, path, expected", [
    ("**/__pycache__", "src/a/__pycache__", True),
    ("**/__pycache__", "__pycache__", True),
    ("**/*.pyc", "src/a/b.pyc", True),
    ("**/*.pyc", "src/a/b.py", False),
    ("src/utils/test", "src/utils/test", True),
    ("src/utils/test", "src/utils/tester", False),
    ("**/test_*.py", "x/test_a.py", True),
    ("*.txt", "a/b.txt", False),                      # a single star never crosses a folder
])
def test_glob_matching(pattern, path, expected):
    assert bb.Matcher([pattern])(path) is expected


def test_exclude_on_a_folder_covers_everything_below_it_and_the_real_test_generator_module_is_kept(tmp_path):
    write(tmp_path, {"src/agents/evaluators/modules/test_generator.py": "x = 1\n", "src/utils/test/t.py": "x = 1\n", "src/a.py": "x = 1\n",
                     "src/__pycache__/a.pyc": "x"})
    spec = bb.load_spec()
    kept = {rel for rel, _ in bb.iter_files(tmp_path, ["src"], spec["slai"]["exclude"])}
    assert kept == {"src/agents/evaluators/modules/test_generator.py", "src/a.py"}


def test_iter_files_is_stable_and_skips_missing_entries(tmp_path):
    write(tmp_path, {"b/2.py": "", "b/1.py": "", "a.py": ""})
    first = [rel for rel, _ in bb.iter_files(tmp_path, ["b", "a.py", "nope"], [])]
    assert first == ["b/1.py", "b/2.py", "a.py"]


# ----------------------------------------------------------------------------------------------------------------------------------- staging
def test_prepare_output_only_cleans_its_own_folders(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(bb.BuildError):
        bb.prepare_output(foreign)
    assert (foreign / "precious.txt").is_file()
    mine = tmp_path / "mine"
    bb.prepare_output(mine)
    (mine / "old.txt").write_text("x", encoding="utf-8")
    bb.prepare_output(mine)
    assert not (mine / "old.txt").exists() and (mine / bb.MARKER).is_file()


def test_missing_sources_are_all_named_at_once(tmp_path):
    spec = bb.load_spec()
    with pytest.raises(bb.BuildError) as caught:
        bb.check_sources(tmp_path, tmp_path, spec)
    message = str(caught.value)
    assert "src/agents/agent_factory.py" in message and "dist/index.html" in message and "npm run build" in message


def test_the_real_spec_is_consistent():
    spec = bb.load_spec()
    assert spec["slai"]["required"] and spec["slai"]["entry_modules"]
    assert all("test_*.py" not in pattern for pattern in spec["slai"]["exclude"])


# -------------------------------------------------------------------------------------------------------------------- import-closure safety net
def test_closure_follows_lazy_relative_and_package_imports(tmp_path):
    write(tmp_path, {
        "src/__init__.py": "", "src/agents/__init__.py": "",
        "src/agents/factory.py": "def make():\n    from .spatial import S\n    from src.utils import helper\n    return S, helper\n",
        "src/agents/spatial.py": "try:\n    import src.utils.optional as o\nexcept ImportError:\n    o = None\nclass S: pass\n",
        "src/agents/unused.py": "x = 1\n",
        "src/utils/__init__.py": "", "src/utils/helper.py": "import numpy\n", "src/utils/optional.py": "",
    })
    closure = bb.local_closure(tmp_path, ["src.agents.factory"])
    assert {"src/agents/factory.py", "src/agents/spatial.py", "src/utils/helper.py", "src/utils/optional.py", "src/__init__.py",
            "src/agents/__init__.py", "src/utils/__init__.py"} <= closure
    assert "src/agents/unused.py" not in closure
    assert not any("numpy" in rel for rel in closure)          # third-party imports are not SLAI files


def test_an_exclude_that_removes_imported_code_fails_the_build(tmp_path):
    slai, out = tmp_path / "slai", tmp_path / "out"
    files = {"src/__init__.py": "", "src/entry.py": "from src import needed\n", "src/needed.py": "x = 1\n"}
    write(slai, files)
    write(out / bb.SLAI_DIR, {k: v for k, v in files.items() if k != "src/needed.py"})
    spec = {"slai": {"entry_modules": ["src.entry"], "include": ["src"]}}
    with pytest.raises(bb.BuildError, match="src/needed.py"):
        bb.check_reachable(slai, out, spec)
    write(out / bb.SLAI_DIR, {"src/needed.py": "x = 1\n"})
    count, warnings = bb.check_reachable(slai, out, spec)
    assert count == 3 and warnings == []


def test_code_in_unshipped_folders_is_only_a_warning(tmp_path):
    slai, out = tmp_path / "slai", tmp_path / "out"
    write(slai, {"src/__init__.py": "", "src/entry.py": "import data.thing\n", "data/__init__.py": "", "data/thing.py": ""})
    write(out / bb.SLAI_DIR, {"src/__init__.py": "", "src/entry.py": "import data.thing\n"})
    _count, warnings = bb.check_reachable(slai, out, {"slai": {"entry_modules": ["src.entry"], "include": ["src"]}})
    assert len(warnings) == 1 and "data" in warnings[0]


# ----------------------------------------------------------------------------------------------------------------------------------- runtime
def test_wheels_are_always_resolved_for_the_windows_interpreter():
    flags = bb.target_flags("3.13")
    assert flags == ["--platform", "win_amd64", "--implementation", "cp", "--python-version", "3.13", "--abi", "cp313"]
    cmd = bb.pip_command("py", Path("site"), [Path("r.txt")], ["x"], series="3.13", torch_index="https://example/cpu", constraints=Path("c.txt"))
    assert "--only-binary=:all:" in cmd and "--target" in cmd and "win_amd64" in cmd
    assert cmd[cmd.index("--extra-index-url") + 1] == "https://example/cpu"
    assert cmd[cmd.index("--constraint") + 1] == "c.txt" and cmd[-1] == "x"
    assert "--user" not in cmd and "--break-system-packages" not in cmd                       # nothing is installed on the build machine


def test_python_version_resolution():
    spec = bb.load_spec()
    assert bb.resolve_python_version(spec, "3.13.14") == "3.13.14"
    with pytest.raises(bb.BuildError):
        bb.resolve_python_version(spec, "3.12.9")
    assert bb.embed_url(spec, "3.13.14") == "https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip"


# ----------------------------------------------------------------------------------------------------------------- manifest and verification
def test_manifest_round_trip_and_tamper_detection(tmp_path):
    engine = tmp_path / "engine"
    write(engine, {"slai-root/a.py": "a = 1\n", "slai-root/b/c.py": "c = 2\n", bb.MARKER: "m"})
    files = bb.hash_tree(engine)
    assert set(files) == {"slai-root/a.py", "slai-root/b/c.py"}
    manifest = {"format": support.MANIFEST_FORMAT, "files": files, "app": {"name": "CADCon", "version": "x"}}
    (engine / support.BUNDLE_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    slai_root = engine / "slai-root"
    assert support.read_bundle_manifest(slai_root)["files"] == files
    assert support.verify_bundle(manifest, engine) == (2, [])
    (engine / "slai-root/a.py").write_text("a = 999\n", encoding="utf-8")
    (engine / "slai-root/b/c.py").unlink()
    checked, problems = support.verify_bundle(manifest, engine)
    assert checked == 2 and "changed: slai-root/a.py" in problems and "missing: slai-root/b/c.py" in problems


def test_only_a_folder_named_slai_root_is_a_bundle(tmp_path):
    (tmp_path / support.BUNDLE_MANIFEST).write_text(json.dumps({"format": support.MANIFEST_FORMAT}), encoding="utf-8")
    assert support.read_bundle_manifest(tmp_path / "SLAI") is None
    assert support.read_bundle_manifest(tmp_path / "slai-root") == {"format": support.MANIFEST_FORMAT}
    (tmp_path / support.BUNDLE_MANIFEST).write_text("{broken", encoding="utf-8")
    assert support.read_bundle_manifest(tmp_path / "slai-root") is None


def test_run_artifacts_are_removed_but_hashed_files_stay(tmp_path):
    out = tmp_path / "out"
    write(out, {"slai-root/keep.py": "k\n", "slai-root/logs/runtime/run.log": "noise", "slai-root/evaluation_logs/x.json": "{}",
                "slai-root/__pycache__/k.pyc": "c"})
    manifest = {"files": {"slai-root/keep.py": {}}}
    assert bb.remove_run_artifacts(out, manifest) == 2
    assert (out / "slai-root/keep.py").is_file() and (out / "slai-root/__pycache__/k.pyc").is_file()
    assert not (out / "slai-root/evaluation_logs").exists() and not (out / "slai-root/logs").exists()


def test_human_sizes():
    assert bb.human(512) == "512 B" or bb.human(512).startswith("512")
    assert "MB" in bb.human(5 * 1024 * 1024)


# --------------------------------------------------------------------------------------------------------------------- environment report
def make_slai(root: Path) -> Path:
    write(root, {"src/utils/sldc.py": "", "applications/tools/server/http.py": "", "applications/tools/desktop.py": ""})
    return root


def test_a_missing_slai_is_a_failure(tmp_path):
    report = support.check_environment(slai_root=None, app_dir=tmp_path, client_dir=tmp_path)
    assert not report.ok and any(i.name == "slai" and i.status == support.FAIL for i in report.items)


def test_missing_web_client_fails_and_agents_are_warnings_unless_required(tmp_path):
    slai = make_slai(tmp_path / "slai")
    client = tmp_path / "dist"
    report = support.check_environment(slai_root=slai, app_dir=tmp_path, client_dir=client)
    statuses = {i.name: i.status for i in report.items}
    assert statuses["web client"] == support.FAIL and statuses["agent data"] == support.WARN
    write(client, {"index.html": "<html>", "startup/loader.js": ""})
    assert support.check_environment(slai_root=slai, app_dir=tmp_path, client_dir=client).ok
    strict = support.check_environment(slai_root=slai, app_dir=tmp_path, client_dir=client, require_agents=True)
    assert not strict.ok and {i.name: i.status for i in strict.items}["agent data"] == support.FAIL


def test_agent_health_is_reported_per_agent_and_a_crash_does_not_escape(tmp_path):
    slai = make_slai(tmp_path / "slai")
    write(tmp_path / "dist", {"index.html": "<html>", "startup/loader.js": ""})
    for rel in support.AGENT_DATA:
        write(slai, {rel: ""})

    def health(key):
        if key == "boom":
            raise RuntimeError("No module named 'torch'\nsecond line")
        return {"state": "ready", "class": "SpatialAgent"}

    lenient = support.check_environment(slai_root=slai, app_dir=tmp_path, client_dir=tmp_path / "dist", agent_health=health, agent_keys=["spatial", "boom"])
    rows = {i.name: i for i in lenient.items}
    assert rows["agent spatial"].status == support.OK
    assert rows["agent boom"].status == support.WARN and "torch" in rows["agent boom"].detail and "second line" not in rows["agent boom"].detail
    assert lenient.ok
    strict = support.check_environment(slai_root=slai, app_dir=tmp_path, client_dir=tmp_path / "dist", agent_health=health, agent_keys=["boom"],
                                       require_agents=True)
    assert not strict.ok
    assert "Result: NOT ready" in strict.format() and json.loads(json.dumps(strict.to_dict()))["ok"] is False


def test_a_modified_bundle_fails_the_check(tmp_path):
    engine = tmp_path / "engine"
    slai = make_slai(engine / "slai-root")
    write(engine / "slai-root", {"x.py": "1\n"})
    write(tmp_path / "dist", {"index.html": "<html>", "startup/loader.js": ""})
    files = bb.hash_tree(engine)
    (engine / support.BUNDLE_MANIFEST).write_text(json.dumps({"format": support.MANIFEST_FORMAT, "files": files}), encoding="utf-8")
    args = dict(slai_root=slai, app_dir=tmp_path, client_dir=tmp_path / "dist", verify_files=True)
    assert support.check_environment(**args).ok
    (engine / "slai-root" / "x.py").write_text("2\n", encoding="utf-8")
    report = support.check_environment(**args)
    assert not report.ok and any("changed: slai-root/x.py" in i.detail for i in report.items)


def test_error_line_is_a_single_line(capsys):
    support.announce_error("first\n  second   line")
    assert capsys.readouterr().out == "CADCON_ERROR first second line\n"
