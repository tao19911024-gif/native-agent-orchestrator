"""Create and inspect a standalone example through the installed public CLI."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import sys


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new directory for the example and local records")
    args = parser.parse_args()
    root = args.directory.resolve()
    runtime_version = version("foundry-runtime")
    root.mkdir(parents=True)
    workspace = root / "project"
    shutil.copytree(Path(__file__).with_name("hello-project"), workspace,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    run(["git", "init", "-q", "--initial-branch=main"], workspace)
    run(["git", "add", "."], workspace)
    run(["git", "-c", "user.name=Example Maintainer", "-c",
         "user.email=maintainer@example.invalid", "commit", "-qm", "Add greeting example"], workspace)
    baseline = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=workspace,
                              capture_output=True, text=True, encoding="utf-8", check=True)
    (root / "baseline-tests.txt").write_text(baseline.stdout + baseline.stderr, encoding="utf-8")

    config = root / "project.json"
    cli = [sys.executable, "-I", "-m", "foundry_runtime"]
    registered = json.loads(run([*cli, "--foundry-project", "register",
                                 "--worktree", str(workspace), "--project-config", str(config)], root))
    project = registered["project"]
    project["execution"] = {"owner": "foundry", "preflight_argv":
                            [sys.executable, "-m", "unittest", "-v"]}
    config.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    checked = json.loads(run([*cli, "--foundry-project", "check", "--project-config", str(config)], root))
    status = json.loads(run([*cli, "--foundry-project", "status", "--project-config", str(config)], root))
    for name, report in (("registration", registered), ("inspection", checked), ("status", status)):
        (root / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Runtime: foundry-runtime {runtime_version}")
    print(f"Workspace: {workspace}")
    print("Baseline tests: PASS")
    print(f"Registration: {registered['status']}")
    print(f"Repository identity matches: {checked['identity_matches']}")
    print("Rule sources: " + ", ".join(row["reference"] for row in checked["rule_sources"]))
    print(f"Execution configured: {checked['capabilities']['external_task_execution']}")
    print(f"Recorded task runs: {len(status['runs'])}")
    print(f"Descriptor: {config}")


if __name__ == "__main__":
    main()
