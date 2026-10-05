"""End-to-end checks with an HTTP mock model; no installed LLM is required."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from local_llm_tools.files import FileTools
from local_llm_tools.registry import ToolRegistry
from local_llm_tools.sessions import process_identity
from local_llm_tools.web import fetch_url, register_web_tools


class MockOllamaHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        if self.path != "/api/chat":
            self.send_error(404)
            return
        messages = body["messages"]
        users = [message["content"] for message in messages if message["role"] == "user"]
        if len(users) == 1 and messages[-1]["role"] == "user":
            message = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "create_file", "arguments": {"path": "created.txt", "content": "Written through a real child agent."}}}]}
        elif messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            message = {"role": "assistant", "content": "File created." if result["ok"] else "Tool failed."}
        else:
            message = {"role": "assistant", "content": "Remembered: " + users[0]}
        raw = json.dumps({"message": message, "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        raw = b'<p>Local page</p><script>ignored()</script><a href="/download">file</a>' if self.path == "/page" else b"downloaded bytes"
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/page" else "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class MockModelIntegrationTests(unittest.TestCase):
    def setUp(self):
        MockOllamaHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def test_bash_child_mock_model_creates_file_remembers_and_stops(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "agent-instance.sh"
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            work = Path(temporary) / "work"
            work.mkdir()
            env = dict(os.environ, LOCAL_LLM_PYTHON=sys.executable)
            env.pop("LOCAL_LLM_AGENT_CHILD", None)

            def cli(*args):
                completed = subprocess.run(["bash", str(script), *args, "--state-dir", str(state)],
                                           capture_output=True, text=True, timeout=10, env=env)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            def reply(session, after=0):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = cli("read", session, "--after-id", str(after))
                    for message in result["messages"]:
                        if message["role"] == "error":
                            self.fail(message["content"])
                        if message["role"] == "assistant":
                            return message
                    time.sleep(0.05)
                self.fail("Child did not produce a reply within five seconds")

            child = None
            try:
                task = "Create the file; literal shell text: $(echo harmless)"
                child = cli("start", "--model", "mock-test", "--base-url", self.url,
                            "--root", str(work), "--task", task)
                first = reply(child["id"])
                self.assertEqual(first["content"], "File created.")
                self.assertEqual((work / "created.txt").read_text(), "Written through a real child agent.")
                sent = cli("send", child["id"], "What was my original task?")
                second = reply(child["id"], first["id"])
                self.assertEqual(second["reply_to"], sent["message_id"])
                self.assertEqual(second["content"], "Remembered: " + task)
                self.assertEqual(len(MockOllamaHandler.requests), 3)
                self.assertTrue(any(tool["function"]["name"] == "create_file" for tool in MockOllamaHandler.requests[0]["tools"]))
                stopped = cli("stop", child["id"])
                self.assertEqual(stopped["status"], "stopped")
                self.assertIsNone(process_identity(child["pid"]))
            finally:
                if child and process_identity(child["pid"]) is not None:
                    try:
                        cli("stop", child["id"])
                    finally:
                        if process_identity(child["pid"]) is not None:
                            os.kill(child["pid"], signal.SIGKILL)

    def test_real_http_fetch_and_download(self):
        result = fetch_url(self.url + "/page")
        self.assertIn("Local page", result["text"])
        self.assertNotIn("ignored", result["text"])
        self.assertEqual(result["links"], [self.url + "/download"])
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            register_web_tools(registry, FileTools([Path(directory)], approve=lambda *_: True))
            registry.call("download_file", {"url": self.url + "/download", "destination": "result.bin"})
            self.assertEqual((Path(directory) / "result.bin").read_bytes(), b"downloaded bytes")


if __name__ == "__main__":
    unittest.main()
