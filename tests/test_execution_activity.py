import json

from foundry_runtime._provider_activity import execution_activity


def test_member_heartbeats_bind_only_to_their_open_tool():
    from foundry_runtime._provider_activity import ClaudeToolActivity
    activity = ClaudeToolActivity('session')
    def feed(kind, offset, **fields):
        activity.observe({'type': kind, 'session_id': 'session', **fields}, offset)
    def beat(member, elapsed, offset):
        feed('tool_progress', offset, parent_tool_use_id=member, tool_name='Agent',
             heartbeat=True, elapsed_time_seconds=elapsed)
    beat('worker', 900, 0)
    assert not activity.pending and activity.last_progress is None
    feed('assistant', 10, parent_tool_use_id='worker', timestamp='start',
         message={'content':[{'type':'tool_use','id':'one','name':'Bash','input':{'command':'private'}}]})
    beat('worker', 5000, 20)
    beat('peer', 5600, 30)
    assert not activity.pending['one']['inspect_pending_tool']
    beat('worker', 5360, 40)
    assert activity.pending['one']['inspect_pending_tool'] and activity.last_progress is None
    end = {'content':[{'type':'tool_result','tool_use_id':'one','content':'private'}]}
    feed('user', 50, session_id='other', message=end)
    assert 'one' in activity.pending
    feed('user', 60, timestamp='end', message=end)
    beat('worker', 6000, 70)
    assert not activity.pending
    assert activity.last_progress['result_offset'] == 60
    assert 'private' not in json.dumps(activity.last_progress)


def test_bound_codex_tool_wait_expires_on_return_without_timestamp(tmp_path):
    log = tmp_path / 'dispatch.log'
    item = {'type':'commandExecution','id':'cmd'}
    log.write_text(event('item/started', 1000, item) + event('item/completed', 2000, item, thread='peer'))
    assert execution_activity(log, 'app-server', session='root')['active_tools'][0]['tool_id'] == 'cmd'
    with log.open('a') as stream:
        stream.write(event('item/completed', None, item))
    assert execution_activity(log, 'app-server', session='root')['active_tools'] == []


def event(method, at, item=None, thread='root', **extra):
    return 'stdout: ' + json.dumps({'method': method, 'emittedAtMs': at,
        'params': {'threadId': thread, **({'item': item} if item else {}), **extra}}) + '\n'


def test_compaction_output_tools_and_background_errors_are_separate(tmp_path):
    log = tmp_path / 'dispatch.log'
    log.write_text(event('item/completed', 1000, {'type': 'fileChange', 'id': 'edit', 'status': 'completed'}) +
        event('item/started', 2000, {'type': 'contextCompaction', 'id': 'c'}) +
        event('item/completed', 3000, {'type': 'commandExecution', 'id': 'tool', 'exitCode': 0, 'durationMs': 500}) +
        event('item/completed', 671000, {'type': 'contextCompaction', 'id': 'c'}) +
        event('item/completed', 675000, {'type': 'agentMessage', 'id': 'child'}, thread='child') +
        event('thread/tokenUsage/updated', 680000, tokenUsage={'last': {'inputTokens': 109361}}) +
        'stderr: \x1b[2m2026-10-21T09:57:43Z\x1b[0m ERROR failed to refresh available models: request timed out secret=do-not-copy\n')
    before = log.read_bytes()
    result = execution_activity(log, 'app-server', session='root')
    assert result['compaction']['duration_seconds'] == 669
    assert result['last_output']['item_id'] == 'edit'
    assert result['last_tool_completion']['exit_code'] == 0
    assert result['last_usage']['usage']['inputTokens'] == 109361
    assert result['communication_error']['request_timeout']
    assert result['communication_error']['timestamp'] == '2026-10-21T09:57:43Z'
    assert result['last_native_event']['method'] == 'thread/tokenUsage/updated'
    assert 'do-not-copy' not in json.dumps(result)
    assert result['status'] == 'observed' and 'deadlock' not in result
    assert log.read_bytes() == before


def test_truncated_start_partial_event_and_unknown_timestamps(tmp_path):
    log = tmp_path / 'dispatch.log'
    end = event('item/completed', 5000, {'type': 'contextCompaction', 'id': 'c'})
    log.write_text('x' * 4000 + '\n' + end + '{bad}\n' +
                   event('item/completed', 6000, {'type': 'fileChange', 'id': 'partial'}).rstrip())
    result = execution_activity(log, 'app-server', limit=1000)
    assert result['tail_truncated']
    assert result['compaction']['status'] == 'completed'
    assert result['compaction']['duration_seconds'] is None
    assert result['last_output'] is None
    assert execution_activity(tmp_path / 'missing', 'app-server')['status'] == 'unobserved'
    assert execution_activity(log, 'claude')['status'] == 'unobserved'
