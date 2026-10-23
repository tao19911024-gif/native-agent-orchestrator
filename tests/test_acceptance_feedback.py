"""Regression cases from external acceptance of the a93/a94 lifecycle changes."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as goal
from foundry_runtime import _foundry_goal_continuation as continuation
from foundry_runtime import _task_deadline as deadline
from foundry_runtime import _dispatch_capacity as capacity
from foundry_runtime import _launch_context as launch
from foundry_runtime import _foundry_response_contract as contracts
from foundry_runtime._foundry_goal_usage import task_report
from test_delivery_baseline import completed, save


@pytest.mark.parametrize('seconds', [None, 120])
def test_sixty_second_git_observation_uses_task_deadline(monkeypatch, tmp_path, seconds):
    def git(*args, **kwargs):
        timeout = kwargs['timeout']
        assert timeout is None or timeout > 60
        return SimpleNamespace(returncode=0, stdout=b'clean', stderr=b'')
    monkeypatch.setattr(subprocess, 'run', git)
    with deadline.scope(None if seconds is None else deadline.time.time() + seconds):
        assert goal._git(tmp_path, 'status', '--porcelain') == b'clean'


def test_git_timeout_is_named(monkeypatch, tmp_path):
    def git(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(subprocess, 'run', git)
    with pytest.raises(goal.GoalBlocked, match='git observation reached task deadline: status'):
        goal._git(tmp_path, 'status')


@pytest.mark.parametrize('large', [False, True])
def test_unreadable_assignment_returns_unmatched_with_bounded_read(tmp_path, monkeypatch, large):
    package = tmp_path / 'dispatch.package.json'
    log = tmp_path / 'dispatch.log'
    with log.open('wb') as stream:
        stream.write(b'stdout: {"method":"item/started","type":"userMessage",broken\n')
        if large:
            stream.truncate(200 * 1024 * 1024)
    original = Path.open
    reads = []
    class Reader:
        def __enter__(self):
            self.stream = original(log, 'rb')
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            reads.append(size)
            return self.stream.read(size)
    def opening(path, *args, **kwargs):
        return Reader() if path == log else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opening)
    assert not continuation._matches_manager_assignment(package,
        {'turn_binding': {'thread_id': 'thread', 'turn_id': 'turn'}}, {'manager': {'instruction': 'slice'}})
    assert reads == [1024 * 1024]


def test_missing_delivery_keeps_usage_report(tmp_path, monkeypatch):
    from foundry_runtime import _delivery_baseline as baseline
    completed(tmp_path)
    build = baseline.build
    def missing_field(report, read):
        next(row for row in report['records'] if row['kind'] == 'invocation').pop('delivery')
        return build(report, read)
    monkeypatch.setattr(baseline, 'build', missing_field)
    report = task_report([tmp_path])
    assert 'coding' in report
    assert report['delivery_baseline']['coverage'] == 'uncovered'
    assert 'delivery' in report['delivery_baseline']['reason']


@pytest.mark.parametrize('previous', [(1, None), (1,), None])
def test_retry_preserves_original_exception_when_package_unavailable(previous):
    error = ValueError('original admission failure')
    def fail():
        raise error
    with pytest.raises(ValueError) as caught:
        capacity._resume_retaining_boundary(previous, fail)
    assert caught.value is error
    assert not hasattr(error, 'dispatch_package')


def test_deadline_stop_does_not_receive_predecessor_package():
    error = TimeoutError('task deadline precedes provider recovery')
    def fail():
        raise error
    with pytest.raises(TimeoutError) as caught:
        capacity._resume_retaining_boundary((1, 'previous.log'), fail)
    assert caught.value is error and not hasattr(error, 'dispatch_package')


def test_required_review_field_receipt_and_legacy_context(tmp_path):
    context = {'original_order': 'requirement\n' * 1000, 'candidate_commit': 'a'*40,
               'review_field_reads': True}
    verdict = {'candidate_commit': 'a'*40, 'packet_sha256': goal._sha(goal._json_bytes(context)),
               'verdict': 'PASS', 'findings': []}
    with pytest.raises(goal.InvalidReview, match='required field unread.*original_order'):
        goal._validate_review(verdict, context, tmp_path, retained_only=True)
    read = {'pointer': '/context/original_order', 'sha256': goal._sha(goal._json_bytes(context['original_order']))}
    goal._validate_review({**verdict, 'read_fields': [read]}, context, tmp_path, retained_only=True)
    with pytest.raises(goal.InvalidReview, match='digest differs'):
        goal._validate_review({**verdict, 'read_fields': [{**read, 'sha256': '0'*64}]}, context, tmp_path, retained_only=True)
    request = goal._role_request('run', 'review-0', context, context_reference={'path': 'checkpoint'})
    assert contracts.response_contract_for_request(request) == contracts.allocation_contract(
        {'kind': 'review', 'message_recipients': [], 'review_field_reads': True})
    context.pop('review_field_reads')
    verdict['packet_sha256'] = goal._sha(goal._json_bytes(context))
    goal._validate_review(verdict, context, tmp_path, retained_only=True)


def test_role_defaults_and_retained_worker_settings(monkeypatch):
    monkeypatch.delenv('FOUNDRY_TOOL_TOKENS', raising=False)
    worker = launch.capture(enable_index=False)
    reviewer = launch.capture(enable_index=False, root_role='reviewer')
    assert worker['settings']['tool_tokens'] == 3000
    assert reviewer['settings']['tool_tokens'] == 0
    with launch.retained_settings(reviewer['settings']):
        assert launch.capture(enable_index=False)['settings']['tool_tokens'] == 0
    monkeypatch.setenv('FOUNDRY_TOOL_TOKENS', '8000')
    assert launch.capture(enable_index=False, root_role='reviewer')['settings']['tool_tokens'] == 8000
