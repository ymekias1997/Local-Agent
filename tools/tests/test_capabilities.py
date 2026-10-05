"""Verify approval precedes effects and generated files cannot escape isolation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_llm_tools.capabilities import OllamaTools
from local_llm_tools.browser import BrowserTools
from local_llm_tools.desktop import DesktopTools
from local_llm_tools.files import FileTools
from local_llm_tools.sandbox import ScriptSandbox


class CapabilityTests(unittest.TestCase):
    def test_download_denied_before_request(self):
        tools = OllamaTools({}, lambda *_: False)
        tools._request = Mock()
        with self.assertRaises(PermissionError):
            tools.ollama_pull("model")
        tools._request.assert_not_called()

    def test_download_approved_with_model_and_destination(self):
        approvals = []
        def approve(name, arguments):
            approvals.append((name, arguments))
            return True
        tools = OllamaTools({}, approve)
        tools._request = Mock(return_value={"status": "success"})
        self.assertEqual(tools.ollama_pull("example:latest"), {"status": "success"})
        self.assertEqual(approvals[0][0], "ollama_pull")
        self.assertIn("destination", approvals[0][1])
        tools._request.assert_called_once_with("pull", {"model": "example:latest", "stream": False}, timeout=3600)

    def test_delete_approval_required(self):
        tools = OllamaTools({})
        tools._request = Mock()
        with self.assertRaises(PermissionError):
            tools.ollama_delete("model")
        tools._request.assert_not_called()

    def test_uninstalled_model_never_loads(self):
        tools = OllamaTools({})
        tools._request = Mock(return_value={"models": []})
        with self.assertRaises(ValueError):
            tools.ollama_load("missing")
        tools._request.assert_called_once_with("tags")

    def test_switch_calls_runtime(self):
        agent = Mock()
        tools = OllamaTools({}, agent=agent)
        tools._request = Mock(return_value={"models": [{"name": "small:latest"}]})
        tools.ollama_switch("small")
        agent.switch_model.assert_called_once_with("small")

    def test_browser_denial_never_starts_process(self):
        browser = BrowserTools("/tmp/unused-capability-test", lambda *_: False)
        browser._start = Mock()
        browser._page_url = Mock(return_value="https://example.org")
        browser.process = Mock()
        browser.process.poll.return_value = None
        for function, args in [(browser.browser_navigate, ["https://example.org"]),
                               (browser.browser_click, ["button"]), (browser.browser_type, ["input", "text"])]:
            with self.assertRaises(PermissionError):
                function(*args)
        browser._start.assert_not_called()

    def test_browser_rejects_missing_and_changed_page(self):
        browser = BrowserTools("/tmp/unused-capability-test", lambda *_: True)
        with self.assertRaisesRegex(RuntimeError, "No live browser"):
            browser.browser_click("button")
        browser.process = Mock()
        browser.process.poll.return_value = None
        browser._page_url = Mock(side_effect=["https://example.org", "https://changed.example"])
        browser._evaluate = Mock()
        with self.assertRaisesRegex(PermissionError, "changed during approval"):
            browser.browser_click("button")
        browser._evaluate.assert_not_called()

    def test_no_internet_blocks_even_approved_model_download(self):
        tools = OllamaTools({"no_internet": True}, lambda *_: True)
        tools._request = Mock()
        with self.assertRaises(PermissionError):
            tools.ollama_pull("model")
        tools._request.assert_not_called()

    def test_desktop_denial_never_sends_input(self):
        desktop = DesktopTools("/tmp/unused-capability-test", lambda *_: False)
        desktop._executable = Mock(return_value="/usr/bin/wtype")
        desktop._run = Mock()
        desktop._active_window = Mock(return_value={"address": "0x1", "class": "test", "title": "Test"})
        with self.assertRaises(PermissionError):
            desktop.desktop_type("submit")
        desktop._run.assert_not_called()

    def test_snapshot_omits_authority_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "token").write_text("secret")
            (root / "ordinary").write_text("public")
            (root / "link").symlink_to(state / "token")
            sandbox = ScriptSandbox(state, FileTools([root]))
            snapshot = state / "sandbox" / "snapshot"
            snapshot.mkdir(parents=True)
            sandbox._snapshot(snapshot)
            self.assertEqual((snapshot / "root0" / "ordinary").read_text(), "public")
            self.assertFalse((snapshot / "root0" / "state").exists())
            self.assertFalse((snapshot / "root0" / "link").exists())

    def test_commit_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sandbox = ScriptSandbox(root / "state", FileTools([root]))
            run_id = "a" * 32
            workspace = sandbox.directory / run_id / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "safe").write_text("generated")
            sandbox.commit_sandbox_file(run_id, "safe", str(root / "result"))
            self.assertEqual((root / "result").read_text(), "generated")
            (workspace / "link").symlink_to(root / "result")
            with self.assertRaises(PermissionError):
                sandbox.commit_sandbox_file(run_id, "link", str(root / "other"))

    def test_missing_isolation_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = ScriptSandbox(directory, FileTools([]))
            with patch("local_llm_tools.sandbox.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "no host fallback"):
                    sandbox.run_code("python", "print('hello')")


if __name__ == "__main__":
    unittest.main()
