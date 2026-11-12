"""Run one bounded Codex task in an operator-prepared workspace."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


TASK_ID = re.compile(r"AOF-[1-9][0-9]*\Z")
MAX_PROMPT_BYTES = 128 * 1024
MAX_EVENTS_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
CHILD_ENV = {
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "CODEX_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "CODEX_CA_CERTIFICATE",
}


class TaskError(Exception):
    pass


class TaskCancelled(TaskError):
    pass


def child_environment(environ):
    return {key: value for key, value in environ.items() if key in CHILD_ENV}


def workspace_path(value, root):
    root = Path(root).resolve(strict=True)
    workspace = Path(value).resolve(strict=True)
    if workspace == root or root not in workspace.parents or not workspace.is_dir():
        raise TaskError("Workspace must be a repository below the workspace root")
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(workspace),
         "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=10, check=False,
        env=child_environment(os.environ),
    )
    if completed.returncode or Path(completed.stdout.strip()).resolve() != workspace:
        raise TaskError("Workspace must be the root of a Git repository")
    return workspace


def command(workspace, mode, model=None):
    args = [
        "codex", "exec", "--ignore-user-config", "--ephemeral", "--json",
        "--color", "never", "--sandbox", mode, "--cd", str(workspace),
        "--config", 'approval_policy="never"',
        "--config", 'forced_login_method="chatgpt"',
        "--config", 'cli_auth_credentials_store="file"',
        "--config", 'shell_environment_policy.inherit="core"',
    ]
    if model:
        args.extend(["--model", model])
    return args + ["-"]


def result_from_events(events, include_commands=False):
    final = None
    usage = None
    failed = False
    commands = []
    for line in events:
        if len(line) > MAX_EVENT_BYTES:
            raise TaskError("Codex event exceeded the output limit")
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise TaskError("Codex returned an invalid event stream") from None
        if not isinstance(event, dict):
            raise TaskError("Codex returned an invalid event stream")
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            failed = True
        if kind == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                final = item.get("text")
            if isinstance(item, dict) and item.get('type') == 'command_execution':
                commands.append({'command':str(item.get('command',''))[:2000], 'exit_code':item.get('exit_code')})
        if kind == "turn.completed":
            usage = event.get("usage", {})
    if failed or usage is None or not isinstance(final, str) or not final.strip():
        raise TaskError("Codex did not finish the task successfully")
    result = {"response": final, "usage": usage}
    if include_commands:
        result['commands'] = commands[-200:]
    return result


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:
        pass
    finally:
        # The parent may exit before a descendant that ignores SIGTERM. Never
        # leave task commands running after the task has released its lock.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_codex(args, prompt, timeout, environment, cancelled=None, include_commands=False):
    # Raw command output can contain private repository information. Keep it off
    # pod logs and discard it after extracting the final response.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as task_input:
        task_input.write(prompt.encode())
        task_input.seek(0)
        process = subprocess.Popen(
            args, stdin=task_input, stdout=output, stderr=subprocess.DEVNULL,
            env=environment, start_new_session=True,
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled and cancelled():
                    raise TaskCancelled("Task cancelled")
                if time.monotonic() - started > timeout:
                    raise TaskError("Codex task timed out")
                if os.fstat(output.fileno()).st_size > MAX_EVENTS_BYTES:
                    raise TaskError("Codex exceeded the output limit")
                time.sleep(0.2)
            if process.returncode:
                raise TaskError(f"Codex exited with status {process.returncode}")
            if cancelled and cancelled():
                raise TaskCancelled("Task cancelled")
            if os.fstat(output.fileno()).st_size > MAX_EVENTS_BYTES:
                raise TaskError("Codex exceeded the output limit")
            output.seek(0)
            return result_from_events(iter(lambda: output.readline(MAX_EVENT_BYTES + 1), b""), include_commands)
        finally:
            terminate(process)


def execute(options, prompt):
    if not TASK_ID.fullmatch(options.task_id):
        raise TaskError("Invalid Jira task key")
    if not prompt.strip() or len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise TaskError("Task prompt is empty or exceeds the input limit")
    if not 1 <= options.timeout <= 3600:
        raise TaskError("Task timeout must be between 1 and 3600 seconds")
    workspace = workspace_path(options.workspace, options.workspace_root)
    policy = Path(options.policy).read_text(encoding="utf-8")
    if not policy.strip() or len(policy.encode()) > MAX_PROMPT_BYTES:
        raise TaskError("Workspace policy is empty or exceeds the input limit")
    state = Path(options.state_dir).resolve(strict=True)
    if state == workspace or workspace in state.parents or state in workspace.parents:
        raise TaskError("Worker state must be separate from repository workspaces")
    result_file = state / "results" / (options.task_id + ".json")
    result_file.parent.mkdir(mode=0o700, exist_ok=True)
    # Serialize runs sharing a ChatGPT login to avoid concurrent token refreshes.
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskError("Another Codex task is running") from None
        if result_file.exists():
            raise TaskError("A result already exists for this task; review it before retrying")
        environment = child_environment(os.environ)
        environment["CODEX_HOME"] = str(state / "auth")
        environment["TMPDIR"] = str(state / "tmp")
        Path(environment["CODEX_HOME"]).mkdir(mode=0o700, exist_ok=True)
        Path(environment["TMPDIR"]).mkdir(mode=0o700, exist_ok=True)
        content = f"Workspace policy:\n{policy}\n\nTask {options.task_id}:\n{prompt}"
        output = run_codex(command(workspace, options.mode, options.model), content,
                           options.timeout, environment)
        result = {"task_id": options.task_id, "status": "completed", **output}
        with tempfile.NamedTemporaryFile(mode="w", dir=result_file.parent, delete=False) as temp:
            json.dump(result, temp)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp.name, result_file)
    return result_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--workspace-root", default="/workspace")
    parser.add_argument("--state-dir", default="/state")
    parser.add_argument("--policy", default="/policy/AGENTS.md")
    parser.add_argument("--mode", choices=["read-only", "workspace-write"], default="read-only")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=1800)
    options = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
        if len(raw) > MAX_PROMPT_BYTES:
            raise TaskError("Task prompt exceeds the input limit")
        prompt = raw.decode("utf-8")
        path = execute(options, prompt)
    except (TaskError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        # Never echo the task, auth cache, environment, or raw model events.
        print(json.dumps({"status": "failed", "reason": str(error)
                          if isinstance(error, TaskError) else "Worker setup failed"}))
        return 1
    print(json.dumps({"status": "completed", "task_id": options.task_id, "result": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
