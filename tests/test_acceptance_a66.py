"""Closure paths stay short while receipts keep their goal ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as owner
from foundry_runtime._candidate_closure import verify


@pytest.mark.parametrize('exitcode', [0, 3])
def test_deep_goal_closure_git_show_and_cleanup(tmp_path, exitcode):
    tree = tmp_path / 'source'
    tree.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tree), *args]).decode().strip()
    git('init', '-q')
    git('config', 'user.email', 'fixture@example.invalid')
    git('config', 'user.name', 'Fixture')
    git('config', 'core.longpaths', 'false')
    name = 'x' * 150 + '.md'
    (tree / name).write_text('fixture', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'candidate')
    head = git('rev-parse', 'HEAD')
    config_before = (tree / '.git/config').read_bytes()
    folder = tmp_path / ('goal-' + 'g' * 32) / ('round-' + 'r' * 32)
    code = (
        'import subprocess, pathlib, os; '
        f'assert subprocess.check_output(["git", "show", {head + ":" + name!r}]).decode() == "fixture"; '
        'assert subprocess.check_output(["git", "config", "fixture.inherited"]).decode().strip() == "kept"; '
        'print(str(pathlib.Path.cwd())); '
        + ('assert subprocess.check_output(["git", "config", "core.longpaths"]).decode().strip() == "true"; '
           if os.name == 'nt' else '')
        + f'raise SystemExit({exitcode})'
    )
    adapter = SimpleNamespace(candidate_closure_command=lambda checkout, source: [sys.executable, '-c', code])
    env = {**os.environ, 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'fixture.inherited',
           'GIT_CONFIG_VALUE_0': 'kept'}
    result = verify(owner, adapter, tree, head, folder, env)
    assert result['status'] == ('passed' if exitcode == 0 else 'failed'), result
    checkout = Path(result['checkout'])
    assert len(str(checkout)) < 80
    assert not checkout.parent.exists()
    assert result['checkout_cleanup'] == 'removed'
    receipt = Path(result['receipt']['path'])
    assert receipt.is_relative_to(folder)
    assert json.loads(receipt.read_text(encoding='utf-8'))['checkout'] == str(checkout)
    assert (tree / '.git/config').read_bytes() == config_before
    assert git('worktree', 'list', '--porcelain').count('worktree ') == 1
    assert env['GIT_CONFIG_COUNT'] == '1'
