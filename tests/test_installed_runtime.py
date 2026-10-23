"""Exercise installed ownership and continuation without native model calls."""
import importlib
import importlib.metadata
import json
from pathlib import Path
import pkgutil
import subprocess
import sys

import pytest
import foundry_runtime
from foundry_runtime import _compute_selection as compute
from foundry_runtime import _foundry_dispatch as dispatch
from foundry_runtime import _foundry_project as project
from foundry_runtime import _model_catalogue as catalogue
from foundry_runtime import _native_session
from foundry_runtime import _project_status as project_status
from foundry_runtime import foundry_goal as goal
from foundry_runtime._project_execution import open_project
from foundry_runtime.resources import result_receipt_schema


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("PDCL_MODEL_CATALOGUE", "snapshot")
    monkeypatch.setattr(catalogue, "_resolved", {})


def test_all_installed_modules_are_independent():
    assert "site-packages" in str(Path(foundry_runtime.__file__))
    for item in pkgutil.iter_modules(foundry_runtime.__path__):
        if item.name != "__main__":
            importlib.import_module("foundry_runtime." + item.name)
    assert not {"dispatch", "_task_authority", "_pdcl_project", "predispatch"} & sys.modules.keys()
    assert not any("command" in p.lower() or "pdcl" in p.lower() for p in sys.path)
    assert result_receipt_schema()["properties"]["coding_usage"]["type"] == "object"


@pytest.mark.parametrize("module,flag", [("foundry_runtime", "--foundry-goal"),
    ("foundry_runtime.recovery", "--reconcile")])
def test_installed_help_from_unrelated_directory(tmp_path, module, flag):
    result = subprocess.run([sys.executable, "-I", "-m", module, "--help"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert flag in result.stdout


@pytest.mark.parametrize("backend,version", [("claude", 15), ("codex", 20)])
def test_member_lifecycle_installed(backend, version):
    team = compute.resolve_native_team("adaptive", backend=backend)
    assert team["version"] == version
    assert "one member per deliverable" in compute.team_prompt("task", team)
    assert "symbolsOnly=true" in compute.team_prompt("task", team)
    if backend == "claude":
        assert "only for follow-up fixes to its own just-finished work" in team["member_instructions"]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def external(tmp_path, monkeypatch):
    tree = tmp_path / "project"; tree.mkdir()
    git(tree, "init", "-q")
    git(tree, "config", "user.name", "fixture")
    git(tree, "config", "user.email", "fixture@example.invalid")
    (tree / "AGENTS.md").write_text("Project owns acceptance.")
    git(tree, "add", "."); git(tree, "commit", "-qm", "base")
    config = tmp_path / "project.json"
    value = project.register(tree, config)["project"]
    value["execution"] = {"owner": "foundry", "preflight_argv": []}
    config.write_text(json.dumps(value))
    order = tmp_path / "TASK1_fixture.md"; order.write_text("Implement the fixture deliverable.")
    monkeypatch.setattr(dispatch, "_codex", lambda: sys.executable)
    launches = []
    def argv(*args, **kwargs):
        out = Path(args[args.index("-o") + 1]); launches.append(1)
        script = ("import sys; from pathlib import Path; "
                  "Path(" + repr(str(out)) + ").write_text(sys.stdin.read(),encoding='utf-8'); "
                  "print('fixture child completed')")
        return [sys.executable, "-B", "-c", script]
    monkeypatch.setattr(dispatch, "coding_argv", argv)
    return tree, open_project(config), order, launches


def test_installed_dispatch_resume_and_replay(external, monkeypatch):
    tree, scope, order, launches = external
    first = dispatch.dispatch_order(order, project=scope)
    assert first[0] == 0 and not first.replayed
    package_path = first[1].with_suffix(".package.json")
    package_bytes = package_path.read_bytes()
    provenance = json.loads(package_bytes)["runtime_provenance"]
    assert provenance["version"] == importlib.metadata.version('foundry-runtime')
    assert Path(provenance["python"]).resolve() == Path(sys.executable).resolve()
    assert provenance["adapter"]["class"] == "ProjectExecution"
    assert not provenance["gaps"]
    replay = dispatch.dispatch_order(order, project=scope)
    assert replay == first and replay.replayed and replay.intent_id == first.intent_id
    assert package_path.read_bytes() == package_bytes
    monkeypatch.setattr(_native_session, "read_session_worktree", lambda sid: tree)
    result = dispatch.resume_order("original-session", tree, "TASK1", "Fix own work", project=scope)
    assert result[0] == 0
    replay = dispatch.resume_order("original-session", tree, "TASK1", "Fix own work", project=scope)
    assert replay == result and replay.replayed and not result.replayed
    assert len(launches) == 2
    from foundry_runtime._foundry_goal_usage import task_report
    selected = task_report([first[1].with_suffix('.receipt.json'), result[1].with_suffix('.receipt.json')])
    baseline = selected['delivery_baseline']
    assert baseline['summary']['selected_dispatch_count'] == 2
    assert baseline['summary']['invocation_count'] == 0
    assert all(row['execution']['receipt'] == row['reference'] for row in baseline['selected_dispatches'])
    observed = project_status.status(scope.path, "TASK1")
    assert len(observed["runs"]) == 2
    for row in observed["runs"]:
        if row.get("resume_argv"):
            assert row["resume_argv"][1:3] == ["-m", "foundry_runtime"]
            assert row["resume_argv"][0] == sys.executable
    # Status can run from a newer interpreter; commands still target this attempt.
    retained = 'D:/retained-runtime/Scripts/python.exe'
    package_path = result[1].with_suffix('.package.json')
    package = json.loads(package_path.read_bytes())
    package['runtime_provenance']['python'] = retained
    package_path.write_text(json.dumps(package))
    runs = project_status.status(scope.path, 'TASK1')['runs']
    resumed = next(row for row in runs if row['session_id'] == 'original-session')
    assert resumed['resume_argv'][0] == retained
    assert not (tree / ".pdcl-executor.lock").exists()


def test_real_unfinished_attempt_reconcile_uses_admitted_package(external, monkeypatch):
    tree, scope, order, launches = external
    original = dispatch._record_package
    def interrupted(*args):
        original(*args)
        raise RuntimeError('fixture interruption after native admission')
    monkeypatch.setattr(dispatch, '_record_package', interrupted)
    with pytest.raises(RuntimeError, match='fixture interruption'):
        dispatch.dispatch_order(order, project=scope)
    runs = project_status.status(scope.path, 'TASK1')['runs']
    assert len(runs) == 1
    row = runs[0]
    assert row['recorded_state'] == 'unknown' and row['evidence'] == {}
    assert row['reconcile_argv'][0] == sys.executable
    package_path = next((Path(scope.project['evidence_root']) / 'TASK1').glob('*.package.json'))
    package_path.unlink()
    runs = project_status.status(scope.path, 'TASK1')['runs']
    assert 'reconcile_argv' not in runs[0]
    assert runs[0]['recovery_commands']['status'] == 'unavailable'
    assert 'Original dispatch package' in runs[0]['recovery_commands']['reason']


class Analyst:
    max_concurrency = 2
    def __init__(self, *args, **kwargs):
        self.calls = []
    def __call__(self, request):
        self.calls.append(request); packet = request["packet"]
        return {"candidate_commit": packet["candidate_commit"],
                "packet_sha256": request["packet_sha256"], "verdict": "PASS", "findings": []}
    def native_session(self, request):
        return "session-" + request["call_id"][:8]
    def drain(self):
        return {"calls": [{"call_id": r["call_id"], "kind": "review", "backend": "codex",
                           "outcome": "success", "usage": None} for r in self.calls]}
    def cancel(self, reason):
        pass


def test_installed_goal_and_terminal_recovery(external, tmp_path, monkeypatch):
    tree, scope, order, launches = external
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] += "; import subprocess; Path('candidate.txt').write_text('done'); subprocess.run(['git','add','candidate.txt'],check=True); subprocess.run(['git','commit','-qm','candidate'],check=True); print('session id: fixture-session')"
        return argv
    monkeypatch.setattr(dispatch, "coding_argv", coding)
    monkeypatch.setattr(goal, "NativeAnalyst", Analyst)
    folder = tmp_path / "goal"
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher="exec", project_record=scope.record())
    assert result["status"] == "candidate_verified"
    assert len(result["reviews"]) == 2
    saved = (folder / "manifest.json").read_bytes()
    assert json.loads(saved)["runtime_provenance"]["version"] == importlib.metadata.version('foundry-runtime')
    assert goal.recover_goal(order, folder)["status"] == "candidate_verified"
    assert len(launches) == 1
    assert (folder / "manifest.json").read_bytes() == saved


def test_missing_distribution_is_an_observation_gap(monkeypatch):
    from foundry_runtime import _runtime_provenance as provenance
    def missing(name):
        raise provenance.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(provenance.metadata, "distribution", missing)
    value = provenance.observe()
    assert value["version"] is None and value["gaps"]


def test_adapter_provenance_uses_selected_owner():
    from foundry_runtime import _runtime_provenance as provenance
    from foundry_runtime._project_context import adapter_scope
    class Policy:
        pass
    with adapter_scope(Policy()):
        value = provenance.observe()
    assert value["adapter"]["class"].endswith("Policy")
    assert value["adapter"]["path"] == str(Path(__file__).resolve())
    assert len(value["adapter"]["sha256"]) == 64


def test_source_shadowing_is_reported_without_rejecting_execution(monkeypatch, tmp_path):
    from foundry_runtime import _runtime_provenance as provenance
    real = provenance.metadata.distribution("foundry-runtime")
    class DifferentLocation:
        version = real.version
        def locate_file(self, name):
            return tmp_path / name
        def read_text(self, name):
            return real.read_text(name)
    monkeypatch.setattr(provenance.metadata, "distribution", lambda name: DifferentLocation())
    result = provenance.observe()
    assert "imported package root differs from installed distribution" in result["gaps"]


def test_removed_private_entry_modules_are_not_installed():
    assert importlib.util.find_spec("foundry_runtime._foundry_cli") is None
    assert importlib.util.find_spec("foundry_runtime._recovery_producer") is None
