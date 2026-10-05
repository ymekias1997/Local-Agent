import email.message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from local_llm_tools.files import FileTools
from local_llm_tools.registry import ToolRegistry
from local_llm_tools.system import execute_script, notify_user, register_system_tools
from local_llm_tools.web import SafeRedirects, fetch_url, register_web_tools, request_bytes, validate_url


class Response(io.BytesIO):
    def __init__(self, body, content_type="text/html"):
        super().__init__(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return "https://example.com/final/"


class WebToolsTests(unittest.TestCase):
    def test_html_extract_and_links(self):
        response = Response(b'<h1>Hello</h1><script>bad()</script><style>hide</style><p>World &amp; more</p><a href="../next">next</a>')
        with patch("local_llm_tools.web.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = fetch_url("https://example.com/")
        self.assertNotIn("bad", result["text"])
        self.assertNotIn("hide", result["text"])
        self.assertIn("World & more", result["text"])
        self.assertEqual(result["links"], ["https://example.com/next"])

    def test_url_and_redirect_restrictions(self):
        for url in ["file:///etc/passwd", "https://user:secret@example.com", "ftp://example.com"]:
            with self.assertRaises(ValueError):
                validate_url(url)
        original = Request("https://a.example/", headers={"X-Subscription-Token": "secret", "Authorization": "secret"})
        redirect = SafeRedirects().redirect_request(original, None, 302, "Moved", {}, "https://b.example/")
        self.assertFalse(redirect.headers)
        with self.assertRaises(ValueError):
            SafeRedirects().redirect_request(original, None, 302, "Moved", {}, "file:///etc/passwd")

    def test_oversized_http_response(self):
        with patch("local_llm_tools.web.build_opener") as opener:
            opener.return_value.open.return_value = Response(b"123456")
            with self.assertRaises(ValueError):
                request_bytes("https://example.com", 5)

    def test_slow_stream_checks_total_deadline(self):
        response = Response(b"123456")
        with patch("local_llm_tools.web.build_opener") as opener, patch("local_llm_tools.web.time.monotonic", side_effect=[0, 1, 31]):
            opener.return_value.open.return_value = response
            with self.assertRaises(TimeoutError):
                request_bytes("https://example.com", 100)

    def test_download_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            register_web_tools(registry, FileTools([Path(directory)], approve=lambda *_: True))
            with patch("local_llm_tools.web.request_bytes", return_value=(b"data", None, "https://example.com")) as request:
                registry.call("download_file", {"url": "https://example.com", "destination": "file"})
                with self.assertRaises(FileExistsError):
                    registry.call("download_file", {"url": "https://example.com", "destination": "file"})
                self.assertEqual(request.call_count, 1)
            self.assertEqual((Path(directory) / "file").read_bytes(), b"data")

    def test_search_configuration_and_json(self):
        registry = ToolRegistry()
        register_web_tools(registry)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SEARXNG_URL"):
                registry.call("search_web", {"query": "hello"})
        data = json.dumps({"results": [{"title": "a", "url": "https://example.com", "content": "sample"}]}).encode()
        with patch.dict(os.environ, {"SEARXNG_URL": "http://localhost:8080"}, clear=True), patch("local_llm_tools.web.request_bytes", return_value=(data, None, "")) as request:
            result = registry.call("search_web", {"query": "hello there"})
            self.assertIn("q=hello+there", request.call_args.args[0])
            self.assertEqual(result["results"][0]["snippet"], "sample")


class SystemToolsTests(unittest.TestCase):
    def test_registered_script_requires_isolation_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "echo.py"
            script.write_text("import sys\nprint(sys.argv[1])\n")
            registry = ToolRegistry()
            register_system_tools(registry, None, {"echo": script})
            with self.assertRaises(PermissionError):
                registry.call("run_script", {"name": "echo"})
            registry = ToolRegistry()
            sandbox = Mock()
            sandbox.run_code.return_value = {"returncode": 0, "stdout": "isolated"}
            register_system_tools(registry, lambda *_: True, {"echo": script}, sandbox=sandbox)
            value = "$(touch malicious); spaces"
            result = registry.call("run_script", {"name": "echo", "arguments": [value]})
            sandbox.run_code.assert_called_once_with("python", script.read_text(), [value])
            self.assertEqual(result["returncode"], 0)

    def test_script_timeout_and_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "script.py"
            script.write_text("import time\ntime.sleep(10)\n")
            self.assertTrue(execute_script(script, [], timeout=0.1)["timed_out"])
            script.write_text("print('x' * 100000)\n")
            result = execute_script(script, [], max_output=100)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["stdout"]), 100)

    def test_notification_fallback_does_not_write_stdout(self):
        with patch("local_llm_tools.system.shutil.which", return_value=None), patch("sys.stdout", new_callable=io.StringIO) as output:
            result = notify_user("title", "message")
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(result["message"], "message")


if __name__ == "__main__":
    unittest.main()
