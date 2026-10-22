"""Task Scheduler action: enter the original Foundry CLI in this process."""
import json
import os
from pathlib import Path
import runpy
import sys
import traceback

from ._hosted import active_directory, read, write


def run(directory):
    from ._executor_lifecycle import _api, _creation
    directory = Path(directory).resolve(strict=True)
    request = read(directory / 'request.json')
    # Open real files before entering any business code; nothing inherits the
    # submitting desktop's streams. Pythonw initially has no Python std streams.
    # Reserve possibly missing CRT descriptors 0/1/2 before opening real files.
    reserved = [os.open(os.devnull, os.O_RDWR) for _ in range(3)]
    sys.stdout = (directory / 'controller.log').open('a', encoding='utf-8', buffering=1)
    sys.stderr = sys.stdout
    sys.stdin = open(os.devnull, 'r', encoding='utf-8')
    os.dup2(sys.stdin.fileno(), 0)
    os.dup2(sys.stdout.fileno(), 1)
    os.dup2(sys.stdout.fileno(), 2)
    for descriptor in reserved:
        if descriptor > 2:
            os.close(descriptor)
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
    for kind, descriptor in ((-10, 0), (-11, 1), (-12, 2)):
        if not kernel.SetStdHandle(kind, msvcrt.get_osfhandle(descriptor)):
            raise ctypes.WinError(ctypes.get_last_error())
    api = _api()
    started = {'pid': os.getpid(), 'creation_time': _creation(api, api.GetCurrentProcess()),
               'run_id': request['run_id']}
    try:
        with (directory / 'started.json').open('x', encoding='utf-8') as stream:
            json.dump(started, stream)
    except FileExistsError:
        return 1  # No replay, including a manually repeated OS action.
    token = active_directory.set(directory)
    code = 1
    try:
        os.chdir(request['cwd'])
        os.environ.update(request['environment'])
        for key in ('CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_SESSION_ID', 'CODEX_PERMISSION_PROFILE',
                    'FOUNDRY_HOSTED_DIRECTORY'):
            os.environ.pop(key, None)
        # pythonw and python share one venv. Keep provenance and all downstream
        # interpreter arguments pinned to the selected console interpreter.
        sys.executable = request['python']
        argv = list(request['argv'])
        for name, values in request['inputs'].items():
            path = directory / (name.removeprefix('--') + '.json')
            write(path, values)
            index = next(i for i, argument in enumerate(argv) if argument.split('=', 1)[0] == name)
            if '=' in argv[index]:
                argv[index] = name + '=' + str(path)
            else:
                argv[index + 1] = str(path)
        if request['entry']:
            sys.argv = [request['entry'], *argv]
            sys.path.insert(0, str(Path(request['entry']).parent))
            runpy.run_path(request['entry'], run_name='__main__')
            code = 0
        else:
            from .cli import main
            code = main(argv) or 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
        if not isinstance(exc.code, (int, type(None))):
            print(exc.code, file=sys.stderr)
    except BaseException:
        traceback.print_exc()
    finally:
        write(directory / 'finished.json', dict(started, returncode=code))
        active_directory.reset(token)
        sys.stdout.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1]))
