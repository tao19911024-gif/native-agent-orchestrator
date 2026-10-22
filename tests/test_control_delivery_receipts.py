"""Real loopback acknowledgment timing, never a business task or model call."""
import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from foundry_runtime import app_server_transport as transport
from foundry_runtime import _foundry_dispatch as dispatch


def fixture_server(steer):
    app = SimpleNamespace(thread_id='root', turn_id='turn',
        process=SimpleNamespace(pid=123, creation_time=456, contains=lambda *a: True),
        steer=steer)
    server = transport.CorrectionServer(app, attempt_id='attempt')
    handle = {'thread_id':'root', 'turn_id':'turn', 'attempt_id':'attempt',
        'root_creation_time':456, 'worktree':'fixture',
        'control':{'host':server.host,'port':server.port}}
    server.start()
    return server, handle


def test_slow_ack_exceeding_two_seconds_is_received_without_resending():
    calls = []
    def steer(text, turn):
        calls.append((text, turn))
        time.sleep(2.3)  # Explicit regression for the former hardcoded 2 s cap.
        return {'turnId':turn}
    server, handle = fixture_server(steer)
    try:
        result = dispatch.deliver_steer('FIXTURE', '示例应用 单次消息',
            handle=handle, gate=lambda *a: None, timeout=10)
        assert result == {'ok':True, 'turn_id':'turn'}
        assert calls == [('示例应用 单次消息', 'turn')]
    finally:
        server.close()


def test_lost_ack_is_unknown_even_when_input_was_admitted():
    accepted, release = threading.Event(), threading.Event()
    calls = []
    def steer(text, turn):
        calls.append(text); accepted.set()
        release.wait(5)
        return {'turnId':turn}
    server, handle = fixture_server(steer)
    try:
        with pytest.raises(dispatch.SteerRefused, match='delivery unconfirmed') as error:
            dispatch.deliver_steer('FIXTURE', 'once', handle=handle,
                gate=lambda *a: None, timeout=.1)
        assert accepted.wait(2)
        assert 'no active turn' not in str(error.value)
        assert 'before resending' in str(error.value)
        assert calls == ['once']
    finally:
        release.set()
        server.close()


def test_server_rpc_timeout_is_not_a_stale_turn():
    def steer(*args):
        raise TimeoutError('native acknowledgment missing')
    server, handle = fixture_server(steer)
    try:
        result = transport.send_correction(handle, 'once')
        assert result['ok'] is False
        assert result['condition'] == 'delivery unconfirmed'
    finally:
        server.close()


def test_steer_rpc_timeout_keeps_executor_running():
    app = transport.AppServer(None, io.StringIO(), deadline=None)
    app.thread_id, app.turn_id = 'root', 'turn'
    app.policy['rpc_seconds'] = .01
    app._send = lambda *a, **kw: None
    with pytest.raises(TimeoutError):
        app.steer('once', 'turn')
    assert not app._stopping.is_set()
    assert not app._pending


@pytest.mark.parametrize('reply', [[], {}, {'ok':True,'turn_id':'foreign'}])
def test_malformed_ack_is_not_success(reply):
    server, handle = fixture_server(lambda *a: {})
    server._handle = lambda payload: reply
    try:
        with pytest.raises(ValueError, match='acknowledgment'):
            transport.send_correction(handle, 'once')
    finally:
        server.close()


def test_request_frame_keeps_its_short_bound():
    left, right = socket.socketpair()
    try:
        left.settimeout(.02)
        with pytest.raises(TimeoutError):
            transport.CorrectionServer._read_line(left)
    finally:
        left.close(); right.close()
