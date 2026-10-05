"""Real worker lifecycle tests with a deterministic in-process model substitute."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from local_llm_tools.sessions import SessionManager


FAKE_WORKER = """
import runpy, sys, types, time
fake = types.ModuleType('local_llm_tools.agent')
class Agent:
    def __init__(self, config, **kwargs):
        assert config['children'] is False
        self.messages = []
    def run(self, task):
        if task == 'briefly wait':
            time.sleep(.3)
        if task == 'slow':
            time.sleep(60)
        if task == 'fail':
            raise RuntimeError('deliberate failure')
        self.messages.append(task)
        return '|'.join(self.messages)
fake.Agent = Agent
sys.modules['local_llm_tools.agent'] = fake
sys.argv = ['worker'] + sys.argv[1:]
runpy.run_module('local_llm_tools.worker', run_name='__main__')
"""


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(Path(self.temp.name), {"model": "fake", "roots": [self.temp.name]})
        self.children = []
        original_popen = subprocess.Popen

        def popen(argv, **kwargs):
            child = original_popen([sys.executable, "-c", FAKE_WORKER, *argv[-2:]], **kwargs)
            self.children.append(child)
            return child

        self.patch = patch("local_llm_tools.sessions.subprocess.Popen", side_effect=popen)
        self.patch.start()

    def tearDown(self):
        for session in self.manager.list():
            self.manager.stop(session["id"])
        for child in self.children:
            child.wait(timeout=5)
        self.patch.stop()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.03)
        self.fail("Worker did not reach expected state")

    def test_queue_conversation_and_failed_task_recovery(self):
        session_id = self.manager.start("one")["id"]
        self.manager.send(session_id, "two")
        self.manager.send(session_id, "fail")
        self.manager.send(session_id, "three")

        def complete():
            messages = self.manager.read(session_id, limit=100)["messages"]
            replies = [m for m in messages if m["role"] != "user"]
            return replies if len(replies) == 4 else None

        replies = self.wait_for(complete)
        self.assertEqual([m["content"] for m in replies],
                         ["one", "one|two", "RuntimeError: deliberate failure", "one|two|three"])
        self.assertEqual(self.manager.read(session_id, replies[-1]["id"])["messages"], [])

    def test_four_worker_limit_and_reuse_after_stop(self):
        sessions = [self.manager.start()["id"] for _ in range(4)]
        with self.assertRaisesRegex(ValueError, "At most 4"):
            self.manager.start()
        self.assertEqual(self.manager.stop(sessions[0])["status"], "stopped")
        self.assertEqual(self.manager.start()["status"], "starting")

    def test_stop_cancels_running_and_queued_tasks(self):
        session_id = self.manager.start("slow")["id"]
        self.manager.send(session_id, "pending")
        self.wait_for(lambda: self.manager.status(session_id)["status"] == "busy")
        self.assertEqual(self.manager.stop(session_id)["status"], "stopped")
        messages = self.manager.read(session_id)["messages"]
        self.assertEqual([m["status"] for m in messages], ["cancelled", "cancelled"])
        with self.assertRaisesRegex(ValueError, "not running"):
            self.manager.send(session_id, "late")

    def test_pid_identity_mismatch_does_not_signal_process(self):
        session_id = self.manager.start()["id"]
        self.wait_for(lambda: self.manager.status(session_id)["status"] == "idle")
        with self.manager.connect() as db:
            db.execute("UPDATE sessions SET identity='wrong' WHERE id=?", (session_id,))
        self.assertEqual(self.manager.stop(session_id)["status"], "stopped")
        self.assertIsNone(self.children[0].poll())
        self.children[0].terminate()

    def test_recursion_disabled(self):
        self.manager.config["children"] = False
        with self.assertRaises(PermissionError):
            self.manager.start()

    def test_wait_returns_reply_and_bounded_output_is_marked(self):
        session_id = self.manager.start("briefly wait")["id"]
        start = time.monotonic()
        result = self.manager.read(session_id, wait_seconds=3)
        self.assertGreater(time.monotonic() - start, .1)
        self.assertEqual(result["messages"][-1]["role"], "assistant")
        self.manager.send(session_id, "x" * 20_000)
        result = self.manager.read(session_id, result["next_after_id"], wait_seconds=3)
        reply = result["messages"][-1]
        self.assertEqual(reply["role"], "assistant")
        self.assertTrue(reply["content_truncated"])
        self.assertEqual(len(reply["content"]), 12_000)
        self.assertGreater(reply["content_length"], 20_000)

    def test_invalid_task_does_not_launch_process(self):
        with self.assertRaises(ValueError):
            self.manager.start("  ")
        self.assertEqual(self.children, [])


if __name__ == "__main__":
    unittest.main()
