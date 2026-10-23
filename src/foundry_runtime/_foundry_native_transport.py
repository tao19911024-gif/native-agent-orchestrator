"""Native analyst arguments/environment consume the invocation's selected profile."""
from ._compute_selection import compute_for_kind


def same_argv(recorded, current):
    """Only normalise Windows path spelling inside the known Foundry hook."""
    import json
    import os
    import shlex
    def normalise(args):
        args = list(args)
        if os.name != 'nt' or '--settings' not in args:
            return args
        index = args.index('--settings') + 1
        config = json.loads(args[index])
        for group in config.get('hooks', {}).get('PreToolUse', []):
            for hook in group.get('hooks', []):
                parts = shlex.split(hook.get('command', ''))
                if parts[1:3] == ['-I', '-m'] and len(parts) >= 4 and parts[3] in ('foundry_runtime._claude_tool_input', 'foundry_runtime._tool_discipline'):
                    parts[0] = os.path.normcase(os.path.normpath(parts[0]))
                    hook['command'] = shlex.join(parts)
        args[index] = json.dumps(config, sort_keys=True)
        return args
    try:
        return normalise(recorded) == normalise(current)
    except (ValueError, TypeError, IndexError, AttributeError):
        return False


def bind_profile(identity, profiles, *, backend="codex", launch_context=None):
    from ._launch_context import settings
    return {**identity, "launch_context": launch_context['settings'] if launch_context is not None else settings(backend),
            **({"execution_profiles": profiles} if profiles is not None else {})}


def argv(analyst, claude_argv, folder, attempt_id, schema, kind, *, resume_session=None, persistent=False, launch_context):
    compute = compute_for_kind(analyst._role_compute, kind, analyst._compute)
    profiles = analyst.execution_profiles
    options = {"launch_context": launch_context, **({"resume_session": resume_session} if resume_session else {})}
    if profiles is not None:
        options["execution_policy"] = profiles[analyst.backend]
    if analyst.backend == "codex":
        from .foundry_codex import codex_argv
        return codex_argv(analyst.executable, (folder / "response-schema.json").resolve(),
            workspace=analyst.workspace or folder, **compute, **options,
            **({"persistent": True} if kind == "coordinate" or persistent else {}))
    return claude_argv(analyst.executable, "foundry-analysis-" + attempt_id, schema, workspace=analyst.workspace or folder, **compute, **options)


def environment(analyst, kind, launch_context=None):
    if analyst.backend == "codex":
        from .foundry_codex import codex_backend_env
        return codex_backend_env(analyst.executable)
    from .foundry_claude import environment
    compute = compute_for_kind(analyst._role_compute, kind, analyst._compute)
    return environment(compute.get("effort"), launch_context=launch_context, **({"execution_environment": analyst.execution_profiles["claude_environment"]}
                        if analyst.execution_profiles is not None else {}))


def invocation(analyst, request, prompt, context, schema, now):
    import hashlib
    from ._compute_selection import call_compute_metadata
    from ._launch_context import capture
    folder, allocation = context["folder"], context["allocation"]
    working_dir = analyst.workspace or folder.resolve()
    launch = analyst.launch_context or capture(working_dir,
        root_role=('expert' if request.get('packet', {}).get('stage') == 'expert_consultation' else
                   'manager' if request['kind'] == 'coordinate' else
                   'reviewer' if request['kind'] == 'review' else 'executor'), backend=analyst.backend,
        coding_model=analyst.recorded_coding_model)
    args = analyst._argv(folder, allocation["attempt_id"], schema, request["kind"],
        resume_session=allocation.get("resume_session"), persistent=allocation.get("persistent_session", False),
        launch_context=launch)
    return {"schema_version": 2, "purpose": "native evidence analysis", "argv": args,
        "launch_context": launch, "backend": analyst.backend, "backend_identity": analyst.backend_identity,
        **call_compute_metadata(analyst, request["kind"]), "response_contract": schema,
        "call_id": request.get("call_id"), "cwd": str(working_dir), "attempt_id": allocation["attempt_id"],
        "graph_run_id": request.get("run_id"), "graph_attempt_id": request.get("attempt_id"),
        "order_sha256": request.get("order_sha256"), "stdin_sha256": hashlib.sha256(prompt).hexdigest(),
        "input_bytes": len(prompt), "started_utc": now, "run_timeout_seconds": analyst.timeout_seconds,
        "cleanup_timeout_seconds": analyst.kill_seconds, "max_output_bytes": analyst.max_output_bytes,
        "expected_ports": [], "stop_condition": "native completion, failure, run deadline or explicitly configured output ceiling"}
