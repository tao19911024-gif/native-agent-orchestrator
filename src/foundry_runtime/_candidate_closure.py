"""Fresh candidate checkout verification; projects own the actual check command."""
from dataclasses import asdict
from pathlib import Path
import subprocess
import os
import time
import uuid
import tempfile
import shutil

from ._executor_lifecycle import OwnedProcess, SpawnFailure
from . import _task_deadline


def _run_owned(owner, argv, cwd, root, env, phases, phase):
    """Checkout and project verification share custody and the task deadline."""
    prefix = 'checkout-' if phase == 'checkout' else ''
    paths = {name: root / (prefix + name + '.txt') for name in ('stdout', 'stderr')}
    record = phases[phase] = {'argv': argv, 'started': False}
    started = time.monotonic()
    process = None
    try:
        with paths['stdout'].open('wb') as stdout, paths['stderr'].open('wb') as stderr:
            try:
                _task_deadline.check()
                process = OwnedProcess.spawn(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, env=env, attempt_id=root.name + '-' + phase)
                record['started'] = True
                while (rc := process.poll()) is None:
                    _task_deadline.check()
                    time.sleep(.05)
                record['returncode'] = rc
            except SpawnFailure as exc:
                record['started'] = exc.process is not None
                record['cleanup'] = asdict(exc.cleanup)
                raise
            finally:
                if process is not None:
                    record['cleanup'] = asdict(process.stop_and_verify(drain_seconds=0, kill_seconds=2))
    finally:
        record['elapsed_seconds'] = time.monotonic() - started
        for name, path in paths.items():
            if path.exists():
                record[name] = owner._fact(path)
    return record


def verify(owner, adapter, tree, head, folder, env):
    command = getattr(adapter, 'candidate_closure_command', None)
    if command is None:
        return {'status': 'not_configured', 'candidate_commit': head,
                'reason': 'Project adapter does not declare a fresh-checkout closure command.'}
    root = Path(folder).resolve() / ('candidate-closure-' + uuid.uuid4().hex)
    # Evidence remains under the goal; generated paths must not inherit its depth.
    # C: is reserved for the OS on this host, matching the existing temp policy.
    volume = Path('D:/') if root.drive.lower() == 'c:' else Path(root.anchor)
    base = volume / 'Foundry/tmp' if os.name == 'nt' else Path(tempfile.gettempdir()) / 'foundry-closure'
    work = base / uuid.uuid4().hex
    checkout = work / 'checkout'
    try:
        argv = command(checkout, tree)
    except Exception as exc:
        root.mkdir(parents=True)
        result = {'status': 'not_evaluated', 'candidate_commit': head, 'error': str(exc)}
        owner._write(root/'receipt.json', result)
        return {**result, 'receipt': owner._fact(root/'receipt.json')}
    if not argv:
        return {'status': 'not_configured', 'candidate_commit': head}
    root.mkdir(parents=True)
    temporary = work / 'tmp'
    result = {'candidate_commit': head, 'checkout': str(checkout), 'argv': argv,
              'status': 'not_evaluated', 'phases': {}}
    added = False
    options = ('-c', 'core.longpaths=true') if os.name == 'nt' else ()
    try:
        temporary.mkdir(parents=True)
        env = {**(os.environ if env is None else env), 'TMP': str(temporary), 'TEMP': str(temporary)}
        if os.name == 'nt':
            # Worktrees share --local config with live tasks. Scope this override
            # to this process and its children; preserve prior config entries.
            count = int(env.get('GIT_CONFIG_COUNT', '0'))
            env.update({f'GIT_CONFIG_KEY_{count}': 'core.longpaths',
                        f'GIT_CONFIG_VALUE_{count}': 'true', 'GIT_CONFIG_COUNT': str(count + 1)})
        git_executable = shutil.which('git', path=env.get('PATH'))
        if git_executable is None:
            raise ValueError('candidate checkout requires git on PATH')
        checkout_run = _run_owned(owner,
            [str(Path(git_executable).resolve()), '--no-optional-locks', '-c', 'core.fsmonitor=false', *options,
             'worktree', 'add', '--detach', str(checkout), head],
            tree, root, env, result['phases'], 'checkout')
        if checkout_run['returncode']:
            error = (root / 'checkout-stderr.txt').read_text(encoding='utf-8', errors='replace')
            raise ValueError('candidate checkout failed: ' + error[:1000])
        if not checkout_run['cleanup']['cleanup_verified']:
            raise ValueError('candidate checkout process cleanup unresolved')
        added = True
        if owner._git(checkout, 'rev-parse', 'HEAD').decode().strip() != head:
            raise ValueError('fresh checkout differs from candidate')
        execution = _run_owned(owner, argv, checkout, root, env, result['phases'], 'verification')
        rc = execution['returncode']
        result['process_cleanup'] = execution['cleanup']
        stderr_text = (root/'stderr.txt').read_text(encoding='utf-8', errors='replace')
        stdout_text = (root/'stdout.txt').read_text(encoding='utf-8', errors='replace')
        queue_expired = 'test lane wait ended:' in stderr_text + stdout_text
        result.update(returncode=rc, status=('not_evaluated' if queue_expired else
                      'passed' if rc == 0 and execution['cleanup']['cleanup_verified'] else 'failed'))
        result['stdout'] = owner._fact(root/'stdout.txt')
        result['stderr'] = owner._fact(root/'stderr.txt')
    except Exception as exc:
        if 'verification' in result['phases'] and 'cleanup' in result['phases']['verification']:
            result['process_cleanup'] = result['phases']['verification']['cleanup']
        result.update(status='not_evaluated', error=str(exc))
    finally:
        if work.exists():
            try:
                # The exact freshly-created child checkout is the only removal
                # target. Never clean or reset the executor's working tree.
                if checkout.resolve().parent != work.resolve() or work.resolve().parent != base.resolve():
                    raise ValueError('closure checkout moved')
                if any(phase['started'] and not phase.get('cleanup', {}).get('cleanup_verified')
                       for phase in result['phases'].values()):
                    raise ValueError('closure process cleanup unresolved; retain checkout')
                # A failed add can already have registered this exact worktree.
                # Remove that registration too; never prune unrelated entries.
                registered = added
                if not added and checkout.exists():
                    records = owner._git(tree, 'worktree', 'list', '--porcelain', '-z').split(b'\0')
                    registered = any(Path(row[9:].decode('utf-8')).resolve() == checkout.resolve()
                                     for row in records if row.startswith(b'worktree '))
                if registered:
                    owner._git(tree, *options, 'worktree', 'remove', '--force', str(checkout))
                shutil.rmtree(work)
                result['checkout_cleanup'] = 'removed'
            except Exception as exc:
                result['checkout_cleanup'] = str(exc)
    owner._write(root/'receipt.json', result)
    return {**result, 'receipt': owner._fact(root/'receipt.json')}
