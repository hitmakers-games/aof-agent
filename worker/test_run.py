import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from worker import run


def events(*values):
    return [json.dumps(value).encode() + b"\n" for value in values]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspaces = self.root / "workspace"
        self.repo = self.workspaces / "repo"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy = self.root / "policy"
        self.policy.write_text("Preserve existing data.")
        self.options = argparse.Namespace(
            task_id="AOF-3350", workspace=str(self.repo),
            workspace_root=str(self.workspaces), state_dir=str(self.state),
            policy=str(self.policy), mode="read-only", model=None, timeout=1,
        )

    def test_workspace_rejects_symlink_escape_and_subdirectory(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspaces / "link"
        link.symlink_to(outside)
        with self.assertRaises(run.TaskError):
            run.workspace_path(link, self.workspaces)
        subdir = self.repo / "subdir"
        subdir.mkdir()
        with self.assertRaises(run.TaskError):
            run.workspace_path(subdir, self.workspaces)
        self.assertEqual(run.workspace_path(self.repo, self.workspaces), self.repo)

    def test_credentials_are_not_inherited(self):
        self.assertEqual(run.child_environment({
            "PATH": "/bin", "HTTPS_PROXY": "http://proxy:8888",
            "OPENAI_API_KEY": "secret", "GH_TOKEN": "secret",
            "TELEGRAM_TOKEN": "secret", "KUBECONFIG": "/secret",
        }), {"PATH": "/bin", "HTTPS_PROXY": "http://proxy:8888"})

    def test_cli_is_noninteractive_and_constrained(self):
        args = run.command(self.repo, "workspace-write")
        self.assertIn('approval_policy="never"', args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn("--ignore-user-config", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertEqual(args[-1], "-")

    def test_event_stream_requires_success_and_final_response(self):
        response = {"type": "item.completed", "item": {
            "type": "agent_message", "text": "Done"}}
        completed = {"type": "turn.completed", "usage": {"input_tokens": 10}}
        self.assertEqual(run.result_from_events(events(response, completed)), {
            "response": "Done", "usage": {"input_tokens": 10}})
        for stream in [events(response), events(completed), events(response, completed,
                       {"type": "turn.failed"}), [b"not json\n"], [b"[]\n"]]:
            with self.subTest(stream=stream), self.assertRaises(run.TaskError):
                run.result_from_events(stream)

    def test_timeout_applies_even_when_child_does_not_read_stdin(self):
        with self.assertRaisesRegex(run.TaskError, "timed out"):
            run.run_codex([sys.executable, "-c", "import time; time.sleep(30)"],
                          "x" * run.MAX_PROMPT_BYTES, 0.1, os.environ.copy())

    def test_nonzero_exit_and_excess_output_fail(self):
        with self.assertRaisesRegex(run.TaskError, "status 7"):
            run.run_codex([sys.executable, "-c", "raise SystemExit(7)"], "test", 2,
                          os.environ.copy())
        with patch.object(run, "MAX_EVENTS_BYTES", 100):
            with self.assertRaisesRegex(run.TaskError, "output limit"):
                run.run_codex([sys.executable, "-c", "print('x' * 1000)"], "test", 2,
                              os.environ.copy())

    def test_result_is_private_and_duplicate_does_not_rerun(self):
        with patch.object(run, "run_codex", return_value={"response": "Done", "usage": {}}) as execute:
            result = run.execute(self.options, "Inspect the repository")
            self.assertEqual(json.loads(result.read_text())["status"], "completed")
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertIn("Preserve existing data.", execute.call_args.args[1])
            with self.assertRaisesRegex(run.TaskError, "already exists"):
                run.execute(self.options, "Retry")
            self.assertEqual(execute.call_count, 1)

    def test_invalid_task_and_concurrent_run_do_not_start_codex(self):
        with patch.object(run, "run_codex") as execute:
            self.options.task_id = "../escape"
            with self.assertRaises(run.TaskError):
                run.execute(self.options, "Inspect")
            self.options.task_id = "AOF-3350"
            with (self.state / "worker.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(run.TaskError, "Another Codex task"):
                    run.execute(self.options, "Inspect")
            execute.assert_not_called()

    def test_failed_run_leaves_no_success_result(self):
        with patch.object(run, "run_codex", side_effect=run.TaskError("failed")):
            with self.assertRaises(run.TaskError):
                run.execute(self.options, "Inspect")
        self.assertFalse(list((self.state / "results").iterdir()))


if __name__ == "__main__":
    unittest.main()
