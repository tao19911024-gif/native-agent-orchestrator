"""Native Bash hook: detach tool input, leaving explicit pipelines/redirections intact."""
import json
import sys

PREFIX = "exec </dev/null\n"


def update(event):
    if event.get("tool_name") != "Bash":
        return {}
    original = event.get("tool_input", {})
    command = original.get("command")
    if not isinstance(command, str):
        return {}
    revised = dict(original)
    revised["command"] = command if command.startswith(PREFIX) else PREFIX + command
    # Do not approve, deny or otherwise change the native permission decision.
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": revised}}


if __name__ == "__main__":
    # Native hooks send UTF-8 bytes; -I ignores Python's encoding environment.
    print(json.dumps(update(json.loads(sys.stdin.buffer.read().decode('utf-8')))))
