from foundry_runtime._context_signals import compaction_fill


def event(identity, kind, thread='a', **fields):
    return {'method': 'item/completed', 'params': {'threadId': thread,
        'item': {'id': identity, 'type': kind, **fields}}}


def test_windows_deduplicate_deltas_and_keep_members_separate():
    output = event('1', 'commandExecution', aggregatedOutput='abc')
    result = compaction_fill([output, output,
        {'method': 'item/commandExecution/outputDelta', 'params': {'delta': 'abc'}},
        event('2', 'reasoning', summary=['xy']), event('3', 'contextCompaction'),
        event('4', 'agentMessage', text='hello'),
        event('1', 'mcpToolCall', thread='b', result={'content': [{'text': 'test'}]})])
    a, b = result['members']['a'], result['members']['b']
    assert a['windows'] == [{'tool_output': 3, 'visible_reasoning': 2}]
    assert a['trailing_partial_window'] == {'messages': 5}
    assert b['visible_chars'] == {'tool_output': 4}
    assert 'not tokens' in result['unit']


def test_residual_uses_next_nonzero_request_not_reset_event():
    from foundry_runtime._context_signals import CompactionResidual
    r=CompactionResidual(120000)
    def usage(n):
        return {'method':'thread/tokenUsage/updated','params':{'threadId':'m','tokenUsage':{'last':{'inputTokens':n},'modelContextWindow':475000}}}
    r.observe(usage(118000)); r.observe(usage(0))
    event={'method':'item/completed','params':{'threadId':'m','item':{'type':'contextCompaction','id':'c'}}}
    r.observe(event); r.observe(event); r.observe(usage(0))
    sample=r.observe(usage(104000))
    assert sample['before_input_tokens']==118000
    assert sample['after_input_tokens']==104000 and sample['headroom_tokens']==16000
    assert sample['renewal_recommended']
    assert len(r.members['m']['residuals'])==1
