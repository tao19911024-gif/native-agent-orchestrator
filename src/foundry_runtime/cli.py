"""One Foundry CLI parser; project entries supply their own route/admission adapter."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from ._app_server_policy import add_policy_arguments, policy_from_arguments
from ._task_deadline import normalize as task_deadline
from ._task_identity import normalized_line


def main(argv=None, *, project_adapter=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import _foundry_dispatch as owner
    ap = argparse.ArgumentParser(description="Run or continue Foundry work under the selected project rules.")
    ap.add_argument("--order", help="path to the order; runs the gate chain first")
    from .foundry_entry import add_arguments
    add_arguments(ap)
    from . import _foundry_project
    _foundry_project.add_arguments(ap)
    if project_adapter is not None:
        project_adapter.configure_cli(ap, argv)
    add_policy_arguments(ap)
    ap.add_argument('--task-deadline', type=task_deadline,
                    help='absolute task deadline, ISO-8601 with timezone; preserved across waits and continuation')
    ap.add_argument("--resume", help="session id of a stopped executor")
    ap.add_argument("--steer", help="tag of a running app-server executor")
    ap.add_argument("--steer-goal", help="exact goal directory; queue --steer input for its manager instead of the worker")
    ap.add_argument("--answer-request", type=json.loads,
                    help="native input request ID as a JSON literal, answered through --steer")
    ap.add_argument("--predispatch-receipt", help="admitted continuation receipt for this exact order/worktree")
    ap.add_argument("--answer-from", help="refused resume admission.json to answer in its original judgement thread")
    ap.add_argument("--answer-file", help="explanation of the resume objection; --prompt-file still holds executor instructions")
    ap.add_argument(
        "--launcher", choices=("exec", "app-server", "claude"), default=None,
        help="coding transport; Foundry goal defaults to app-server, direct order/resume to exec. An app-server resume with no "
             "trusted terminal stays unknown; measured cleanup and explicit recovery authorization allow continuation in the original thread",
    )
    ap.add_argument("--worktree", help="required with --resume")
    ap.add_argument("--execution-intent-id", help="expected frozen intent identity for replay/recovery")
    ap.add_argument("--recovery-file", help="explicit operator recovery authorization for --resume, Foundry --recover-goal, or --amend-from; "
                    "never recovers an app-server unknown through an exec header")
    ap.add_argument("--tag", default="", help="line tag, recorded in the lock")
    ap.add_argument("--prompt", default="", help="text sent on resume")
    ap.add_argument("--prompt-file", default="", help="file whose text is sent on resume")
    ap.add_argument('--hosted', default=os.environ.get('FOUNDRY_HOSTED_DIRECTORY'),
                    help='unique launch directory; Windows service custody (or per-call FOUNDRY_HOSTED_DIRECTORY)')
    ap.add_argument('--hosted-status', help='read the exact independently hosted launch, without starting work')
    a = ap.parse_args(argv)
    # Resolve the frozen native owner before hosted admission or goal writes.
    # This inspection never invokes the provider or reconciles business state.
    from . import _foundry_recovery_preflight as recovery_preflight
    try:
        recovery_plan = recovery_preflight.prepare(a, project_adapter=project_adapter)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        ap.error('recovery preflight: ' + str(exc))
    if a.recovery_preflight:
        print(json.dumps(recovery_plan, ensure_ascii=True, indent=2))
        return 0 if recovery_plan and recovery_plan['status'] in {'ready', 'not_applicable'} else 2
    if recovery_plan and recovery_plan['status'] == 'attention_required':
        print(json.dumps(recovery_plan, ensure_ascii=True, indent=2), file=sys.stderr)
        return 2
    if recovery_plan and recovery_plan['status'] in {'ready', 'reconciliation_required'}:
        python = recovery_plan['runtime_python']
        if Path(python).resolve() != Path(sys.executable).resolve():
            print('Recovery uses recorded runtime: ' + python, flush=True)
            return recovery_preflight.execute_recorded(recovery_plan, argv,
                entry=str(Path(sys.argv[0]).resolve()) if project_adapter is not None else None)
    if a.upgrade_runtime:
        from ._foundry_goal_upgrade import route
        try:
            routed = route(a, argv, project_adapter=project_adapter)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            ap.error(str(exc))
        if routed is not None:
            return routed
    if a.hosted_status:
        from ._hosted import status
        print(json.dumps(status(a.hosted_status, plan=a.follow_up_plan), ensure_ascii=True, indent=2))
        return 0
    if a.steer_goal and (not a.steer or a.answer_request is not None):
        ap.error("--steer-goal requires --steer and cannot answer worker --answer-request")
    if project_adapter is not None:
        project_adapter.validate_cli(a, ap)
    from ._project_context import adapter_scope
    with adapter_scope(project_adapter) if project_adapter is not None else nullcontext():
        project_result = _foundry_project.handle(a, ap)
    if project_result is not None:
        return project_result
    from ._project_execution import select_cli
    project = select_cli(a, ap)
    from ._model_catalogue import catalogue_scope
    if project is None and project_adapter is None:
        if a.foundry_usage:
            from .foundry_entry import handle_mode
            return handle_mode(a, ap)
        ap.error("select --project-config or provide the project adapter at the project entry")
    selected = project if project is not None else project_adapter
    launch_scope = getattr(selected, "launch_scope", nullcontext)
    with adapter_scope(selected), catalogue_scope(selected.catalogue_cache), launch_scope(), recovery_preflight.launch_scope(
            recovery_plan.get('environment', {}) if recovery_plan and recovery_plan['status'] in {'ready', 'reconciliation_required'} else {}):
        if a.hosted:
            if a.steer or not (a.order or a.resume or a.recover_goal or a.amend_from):
                ap.error('--hosted requires a dispatch or explicit recovery, not a status/steering command')
            from ._hosted import submit
            remaining, skip = [], False
            for item in argv:
                if skip:
                    skip = False
                elif item == '--hosted':
                    skip = True
                elif not item.startswith('--hosted='):
                    remaining.append(item)
            entry = None
            if project_adapter is not None and project is None:
                # The existing PDCL entry owns its gates and interpreter pin.
                entry = str(Path(sys.argv[0]).resolve(strict=True))
                remaining = ['--runtime-python', sys.executable, *remaining]
            report = submit(a.hosted, remaining, entry=entry)
            print('FOUNDRY_FOLLOW_UP ' + json.dumps(report, ensure_ascii=True), flush=True)
            return 0 if report['status'] in ('pending', 'outcome_available', 'result_available', 'launch_finished') else 1
        return _selected_main(owner, a, ap, project, project_adapter)


def _selected_main(owner, a, ap, project, project_adapter):
    from .foundry_entry import handle_mode
    a.launcher_explicit = a.launcher is not None
    # Only new goal coding inherits project defaults. Explicit selectors
    # and all recorded continuations keep their existing resolution path.
    from ._project_execution import goal_defaults
    defaults = (goal_defaults(project if project is not None else project_adapter)
        if a.foundry_goal and not (a.amend_from or a.recover_goal or getattr(a, '_boundary_continuation', None)) else {})
    a.project_default_provider = (defaults.get('provider')
        if not any((a.provider, a.launcher, a.compute_profile, a.coding_model, a.auto_amend_model)) else None)
    a.disposable_scratch = a.disposable_scratch or defaults.get('disposable_scratch', False)
    if a.provider == "codex" and a.launcher == "claude":
        ap.error("--provider codex cannot use --launcher claude")
    if a.launcher == "claude" and a.provider is None:
        a.provider = "claude"
    if a.provider == "claude" and a.launcher not in (None, "claude"):
        ap.error("--provider claude uses --launcher claude")
    a.launcher = a.launcher or ("claude" if (a.provider or a.project_default_provider) == "claude"
                              else "app-server" if a.foundry_goal or project else "exec")
    policy = policy_from_arguments(a, ap)
    a.app_server_policy = policy
    if project is None:
        project_adapter.announce(a, ap)
    if a.answer_request is not None and (not a.steer or type(a.answer_request) not in (str, int)):
        ap.error("--answer-request requires --steer and a string or integer request ID")
    if a.predispatch_receipt and not (a.order or a.resume):
        ap.error("--predispatch-receipt requires --order or --resume")
    if a.answer_from or a.answer_file:
        if not (a.resume and a.answer_from and a.answer_file) or a.predispatch_receipt:
            ap.error("--answer-from and --answer-file require --resume together, without --predispatch-receipt")

    foundry_result = None
    if project is None or a.foundry_goal or a.foundry_review:
        foundry_result = handle_mode(a, ap, **({"project": project} if project is not None else {"project_adapter": project_adapter}))
    if foundry_result is not None:
        return foundry_result

    if sum(bool(mode) for mode in (a.order, a.resume, a.steer)) != 1:
        owner.die("REFUSE dispatch: pass exactly one of --order, --resume or --steer.")
    if a.recovery_file and not a.resume:
        ap.error("--recovery-file requires --resume")

    if a.steer:
        if a.execution_intent_id:
            ap.error("--execution-intent-id requires --order or --resume")
        if not a.prompt_file:
            ap.error("--steer requires --prompt-file")
        prompt_path = Path(a.prompt_file)
        if not prompt_path.is_file():
            ap.error("--prompt-file does not exist: %s" % prompt_path)
        text = prompt_path.read_text(encoding="utf-8-sig")
        if not text.strip():
            ap.error("--prompt-file is empty")
        try:
            if a.steer_goal:
                from ._goal_intervention import submit
                result = submit(a.steer_goal, a.steer, text, project or project_adapter)
                print(json.dumps(result, ensure_ascii=True, indent=2))
                return 0
            result = owner.deliver_steer(a.steer, text,
                **({'answer_request': a.answer_request} if a.answer_request is not None else {}))
        except owner.SteerRefused as exc:
            print(str(exc))
            return 1
        except (OSError, ValueError, KeyError) as exc:
            print("Steer not delivered: " + str(exc))
            return 1
        print("STEERED %s turn %s: worker transport accepted; team compliance not observed" % (a.steer, result.get("turn_id")))
        return 0

    if a.order:
        order = Path(a.order)
        if not order.is_file():
            owner.die("REFUSE dispatch: no such order %s" % order)
        with (nullcontext() if project is not None else project_adapter.direct_invocation(a)) as receipt:
            result = owner.dispatch_order(order, tag=a.tag, launcher=a.launcher, execution_intent_id=a.execution_intent_id,
                                       **({"project": project} if project is not None else {}),
                                       **({'predispatch_receipt': a.predispatch_receipt} if a.predispatch_receipt else {}),
                                       **({'app_server_policy': policy} if policy is not None else {}),
                                       task_deadline_at=a.task_deadline,
                                       **owner.compute_kwargs(a.coding_model, a.coding_effort,
                                                        backend="claude" if a.launcher == "claude" else "codex"))
            if receipt is not None:
                receipt.append(result)
    else:
        if not a.worktree:
            owner.die("REFUSE dispatch: --resume needs --worktree; a resumed session declares none,\n"
                "  which is exactly why the lock exists.")
        wt = Path(a.worktree.replace("\\", "/"))
        tag = a.tag or "RESUME"
        text = Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else a.prompt
        if not text.strip():
            owner.die("REFUSE dispatch: --resume needs --prompt or --prompt-file saying what changed.")
        with (nullcontext() if project is not None else project_adapter.direct_invocation(a)) as receipt:
            result = owner.resume_order(a.resume, wt, tag, text, execution_intent_id=a.execution_intent_id,
                                  **({"project": project} if project is not None else {}),
                                  recovery_file=a.recovery_file,
                                  **({"answer_from": a.answer_from, "answer_file": a.answer_file} if a.answer_from else {}),
                                  **({"predispatch_receipt": a.predispatch_receipt} if a.predispatch_receipt else {}),
                                  launcher=a.launcher,
                                  **({'app_server_policy': policy} if policy is not None else {}),
                                  task_deadline_at=a.task_deadline,
                                       **owner.compute_kwargs(a.coding_model, a.coding_effort,
                                                        backend="claude" if a.launcher == "claude" else "codex"))
            if receipt is not None:
                receipt.append(result)

    rc, log, out = result
    app_server = (a.order or a.resume) and a.launcher == 'app-server'
    if getattr(result, 'replayed', False):
        print('\nreplayed prior outcome, no turn started; prior outcome=%s; rc=%d' % (result.outcome, rc))
        print('execution intent: %s' % result.intent_id)
        print('An identical request returns its retained result. To continue, submit a new continuation instruction through the existing resume command.')
        if app_server:
            label = 'legacy mapped result; terminal outcome unproven' if getattr(result, 'returncode_source', None) == 'legacy_app_server_mapping' else 'retained result'
            print('app-server %s; native exit code unobserved' % label)
    elif app_server:
        source = getattr(result, 'returncode_source', None)
        if source == 'turn_status':
            print('\napp-server turn %s; mapped result rc=%d; native exit code unobserved'
                  % (result.outcome.removeprefix('turn_'), rc))
        elif source == 'legacy_app_server_mapping':
            print('\napp-server legacy result rc=%d; terminal outcome unproven; native exit code unobserved' % rc)
        else:
            print('\napp-server result rc=%d; terminal evidence unavailable; native exit code unobserved' % rc)
    elif not getattr(result, "replayed", False):
        print("\ncodex exited rc=%d" % rc)
    print("log    : %s" % log)
    if getattr(result, 'outcome', None) == 'provider_capacity_exhausted':
        import subprocess
        command = getattr(result, 'capacity_recovery', {}).get('resume_argv')
        if command:
            print('capacity recovery (after provider recovery; supply prompt file): ' + subprocess.list2cmdline(command))
    print(("retained output (may be partial): %s" if app_server else "report : %s") % out)
    if not app_server and not getattr(result, 'replayed', False):
        owner.report(log, **({"project": project, "tag": normalized_line(Path(a.order)) if a.order else a.tag,
                       "launcher": a.launcher} if project is not None else {}))
        owner.explain_nonzero_exit(rc, log, out)
    return 0 if rc == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
