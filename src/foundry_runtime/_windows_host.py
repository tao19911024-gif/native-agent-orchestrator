"""Windows service custody, not a second work scheduler or retry policy."""
import base64
import json
import os
from pathlib import Path
import subprocess


_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$root = $service.GetFolder('\')
if ($request.operation -eq 'register') {
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'Foundry execution custody: ' + $request.directory
    $definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.LogonType = 3
    $definition.Settings.Enabled = $true
    $definition.Settings.Hidden = $true
    # Service custody must not demote controllers and their workers. The default
    # 7 lowers CPU, I/O and memory priority; 4 makes all three normal.
    $definition.Settings.Priority = 4
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.MultipleInstances = 2
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.AllowDemandStart = $false
    # Register once and start through the service in the same operation. There
    # is no separate Run call with a crash window that could replay a dispatch.
    $null = $definition.Triggers.Create(7)
    $action = $definition.Actions.Create(0)
    $action.Path = $request.executable
    $action.Arguments = $request.arguments
    $action.WorkingDirectory = $request.cwd
    $task = $root.RegisterTaskDefinition($request.name, $definition, 2, $null, $null, 3)
} elseif ($request.operation -eq 'remove') {
    $root.DeleteTask($request.name, 0)
    @{removed=$true} | ConvertTo-Json -Compress
    exit
} else {
    try { $task = $root.GetTask($request.name) }
    catch {
        if ($_.Exception.HResult -eq -2147024894) {
            @{exists=$false} | ConvertTo-Json -Compress
            exit
        }
        throw
    }
}
@{exists=$true; state=[int]$task.State; last_result=$task.LastTaskResult;
  last_run=$task.LastRunTime.ToString('o'); path=$task.Path;
  execution_limit=$task.Definition.Settings.ExecutionTimeLimit;
  priority=$task.Definition.Settings.Priority} | ConvertTo-Json -Compress
'''


def call(operation, request):
    if os.name != 'nt':
        raise OSError('Independent Foundry hosting currently requires Windows Task Scheduler')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
    completed = subprocess.run(
        [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
        input=json.dumps(dict(request, operation=operation), ensure_ascii=False),
        text=True, encoding='utf-8', capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raise OSError('Windows hosting operation failed: ' + completed.stderr[-3000:])
    return json.loads(completed.stdout.lstrip('\ufeff'))


def scheduler_request(directory, request):
    # pythonw avoids a console even when the service launches an interactive-user
    # process. The execution interpreter remains the recorded python.exe.
    pythonw = Path(request['python']).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise OSError('Hosted runtime requires its adjacent pythonw.exe: ' + str(pythonw))
    return {'name': request['task_name'], 'directory': str(directory),
            'executable': str(pythonw), 'cwd': request['cwd'],
            'arguments': subprocess.list2cmdline(
                ['-I', '-m', 'foundry_runtime._hosted_run', str(directory)])}
