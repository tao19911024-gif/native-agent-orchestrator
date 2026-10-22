import json
from pathlib import Path

from foundry_runtime import _project_status as status


def test_active_quota_status_uses_matching_package(tmp_path, monkeypatch):
    config = tmp_path / 'project.json'
    config.write_text('{}')
    project = dict(workspace=str(tmp_path), git_common_dir=str(tmp_path),
                   project_id='fixture', evidence_root=str(tmp_path / 'evidence'))
    journal = tmp_path / 'pdcl-dispatch/task-T/intents/intent'
    journal.mkdir(parents=True)
    (journal / '00000001.json').write_text('{"created_utc":"2026-10-08T00:00:00Z"}')
    folder = tmp_path / 'evidence/T'
    folder.mkdir(parents=True)
    record = dict(state='active', attempt_id='attempt', session_id='s', evidence={},
                  frozen=dict(operation='dispatch', launcher='claude', worktree=str(tmp_path),
                              project_context=dict(project_id='fixture', descriptor=str(config),
                                                   evidence_root=project['evidence_root'])))
    monkeypatch.setattr(status, 'load', lambda _: project)
    monkeypatch.setattr(status, 'identity', lambda _: {})
    monkeypatch.setattr(status.ExecutionIntent, 'read_history', lambda *a, **k: (record, 1, None))
    monkeypatch.setattr(status, '_liveness', lambda _: dict(owner='alive', root='alive'))
    package = folder / 'dispatch.package.json'
    package.write_text(json.dumps(dict(intent_id='intent', attempt_id='attempt',
                                      runtime_provenance=dict(python='original-python', adapter={
                                          'module': '_pdcl_project', 'path': str(tmp_path / 'bin/_pdcl_project.py')}))))
    log = folder / 'dispatch.claude.jsonl'
    def event(state):
        return json.dumps(dict(type='rate_limit_event', session_id='s',
                               rate_limit_info=dict(status=state, resetsAt=1, rateLimitType='five_hour'))) + '\n'
    log.write_text(event('rejected') + '{"type":"tool_progress"}\n')
    row = status.status(config, 'T')['runs'][0]
    assert row['process_observation']['owner'] == 'alive'
    assert row['provider_observation']['status'] == 'quota_blocked'
    assert row['next_action'] == 'wait_for_provider_capacity'
    assert row['provider_observation']['blocked_sessions'][0]['resets_at'] == 1
    # Reset in the past and heartbeat do not constitute restored model capacity.
    log.write_text(event('rejected') + event('allowed') + '{incomplete')
    row = status.status(config, 'T')['runs'][0]
    assert row['provider_observation']['status'] == 'allowed'
    assert row['next_action'] == 'wait'
    log.write_text(event('rejected'))
    record['state'] = 'completed'
    row = status.status(config, 'T')['runs'][0]
    assert row['next_action'] == 'resume_after_provider_reset'
    assert row['resume_argv'][0] == 'original-python'
    monkeypatch.setattr(status, 'identity', lambda _: {k: project[k] for k in ('workspace', 'git_common_dir', 'project_id')})
    adapter_row = status.status(workspace=tmp_path, evidence_root=tmp_path / 'evidence', tag='T')['runs'][0]
    argv = adapter_row['resume_argv']
    assert argv[argv.index('--launcher') + 1] == 'claude'
    assert '--project-config' not in argv
    saved = json.loads(package.read_text())
    saved['launch_context'] = {'settings': {'compact_tokens': 0}}
    package.write_text(json.dumps(saved))
    record['frozen']['launcher'] = 'app-server'
    assert status.status(config, 'T')['runs'][0]['activity_observation']['status'] == 'unobserved'
    (folder / 'dispatch.log').write_text('stdout: ' + json.dumps({'method': 'item/completed',
        'emittedAtMs': 1000, 'params': {'threadId': 's', 'item': {'type': 'fileChange', 'id': 'edit'}}}) + '\n')
    assert status.status(config, 'T')['runs'][0]['activity_observation']['last_output']['item_id'] == 'edit'
    assert status.status(config, 'T')['runs'][0]['compaction']['mode'] == 'native_default'
    saved['launch_context']['settings']['compact_tokens'] = 240000
    package.write_text(json.dumps(saved))
    assert status.status(config, 'T')['runs'][0]['compaction']['token_limit'] == 240000
    package.write_text(package.read_text().replace('attempt', 'other'))
    row = status.status(config, 'T')['runs'][0]
    assert row['provider_observation']['status'] == 'unobserved'


def test_quota_sessions_are_independent_and_partial_event_is_not_read(tmp_path):
    from foundry_runtime._provider_activity import observe
    path = tmp_path / 'dispatch.claude.jsonl'
    rows = [dict(type='rate_limit_event', session_id=s, rate_limit_info={'status': state})
            for s, state in [('a', 'rejected'), ('b', 'allowed')]]
    path.write_text('\n'.join(map(json.dumps, rows)) + '\n' + json.dumps(
        dict(type='rate_limit_event', session_id='a', rate_limit_info={'status': 'allowed'})))
    observed = observe(tmp_path / 'dispatch.log', 'claude')
    assert observed['status'] == 'quota_blocked'
    assert observed['blocked_sessions'][0]['session_id'] == 'a'
    assert observe(tmp_path / 'dispatch.log', 'app-server')['status'] == 'unobserved'


def test_workspace_status_reads_authority_journal_without_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(status, 'identity', lambda _: dict(workspace=str(tmp_path),
                        git_common_dir=str(tmp_path), project_id='authority'))
    observed = status.status(workspace=tmp_path, evidence_root=tmp_path / 'evidence')
    assert observed['runs'] == []
    assert not (tmp_path / 'pdcl-dispatch').exists()


def test_workspace_status_cli_is_read_only(tmp_path):
    import subprocess
    import sys
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    result = subprocess.run([sys.executable, '-B', '-m', 'foundry_runtime',
                             '--foundry-project', 'status', '--worktree', str(tmp_path),
                             '--status-evidence-root', str(tmp_path / 'evidence')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['runs'] == []
    assert not (tmp_path / 'evidence').exists()
