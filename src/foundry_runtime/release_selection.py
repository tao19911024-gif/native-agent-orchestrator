"""Select or maintain installed releases; never modify a retained run."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def select(selection, python):
    """Observe the target interpreter, then atomically replace the shared pointer."""
    probe = (
        "import json,sys; from importlib.metadata import version; "
        "print(json.dumps({'python':sys.executable,"
        "'version':version('foundry-runtime')}))"
    )
    target = json.loads(subprocess.check_output(
        [str(python), "-I", "-c", probe], text=True, encoding="utf-8"
    ))
    target["python"] = str(Path(target["python"]).absolute())
    selection = Path(selection)
    selection.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=selection.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(target, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, selection)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def main(argv=None, *, project_adapter=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, help="shared current.json path")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--python", help="installed versioned interpreter")
    operation.add_argument("--retention-plan", action='store_true', help="read-only runtime dependency inventory")
    operation.add_argument("--prune-release", action='append', help="exact obsolete release directory; refresh inventory for each")
    parser.add_argument('--rollback-python', help='the one qualified rollback interpreter, required for retention')
    parser.add_argument('--evidence-root', action='append', default=[], help='additional evidence root; known roots always included')
    parser.add_argument('--test-run-record', action='append', default=[],
                        help='completed pytest receipt proving its absolute fixture basetemp')
    parser.add_argument('--evidence-review', help='operator-reviewed primary evidence for exact legacy records')
    args = parser.parse_args(argv)
    if args.python:
        result = select(args.selection, args.python)
    else:
        if not args.rollback_python:
            parser.error('--rollback-python is required for retention')
        from . import _release_retention as retention
        options = {'extra_roots': args.evidence_root}
        if project_adapter is not None:
            options['project_adapter'] = project_adapter
        if args.test_run_record:
            options['test_run_records'] = args.test_run_record
        if args.evidence_review:
            options['evidence_review'] = args.evidence_review
        result = (retention.inventory(args.selection, args.rollback_python, **options) if args.retention_plan
                  else retention.prune(args.selection, args.rollback_python, args.prune_release, **options))
    print(json.dumps(result, indent=2))
    if result.get('errors'):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
