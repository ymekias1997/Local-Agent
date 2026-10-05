"""Authority tests: approvals are exact, one-shot, and cannot be self-granted."""
import tempfile
import os
import base64
import unittest
from pathlib import Path
from unittest.mock import patch

from local_llm_tools.core import default_tools
from local_llm_tools.files import FileTools
from local_llm_tools.registry import ToolRegistry
from local_llm_tools.runtime import ApprovalRequired, RuntimeStore, make_approver
from local_llm_tools.web import register_web_tools


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RuntimeStore(self.root / "state")

    def test_approval_is_bound_to_payload_and_consumed_once(self):
        approve = make_approver(self.store, "agent", "task")
        with self.assertRaises(ApprovalRequired) as waiting:
            approve("download_file", {"url": "https://example.com/a", "destination": "a"})
        identifier = waiting.exception.approval_id
        self.store.decide(identifier, True)
        # Changing the URL cannot reuse the human's approval of a different URL.
        with self.assertRaises(ApprovalRequired):
            approve("download_file", {"url": "https://example.com/b", "destination": "a"})
        self.assertTrue(approve("download_file", {"url": "https://example.com/a", "destination": "a"}))
        self.assertEqual(self.store.get_approval(identifier)["status"], "consumed")
        with self.assertRaises(ApprovalRequired) as repeated:
            approve("download_file", {"url": "https://example.com/a", "destination": "a"})
        self.assertNotEqual(identifier, repeated.exception.approval_id)

    def test_pending_survives_store_restart_and_denial(self):
        with self.assertRaises(ApprovalRequired) as waiting:
            make_approver(self.store, "a", "t")("delete_file", {"path": "one"})
        restarted = RuntimeStore(self.root / "state")
        restarted.decide(waiting.exception.approval_id, False)
        with self.assertRaises(PermissionError):
            make_approver(restarted, "a", "t")("delete_file", {"path": "one"})

    def test_download_waits_before_network_or_creation(self):
        files = FileTools([self.root], make_approver(self.store, "a", "t"), protected_paths=[self.root / "state"])
        registry = ToolRegistry()
        register_web_tools(registry, files)
        arguments = {"url": "https://example.com/file", "destination": "new.txt"}
        with patch("local_llm_tools.web.request_bytes", return_value=(b"ok", None, arguments["url"])) as request:
            with self.assertRaises(ApprovalRequired) as waiting:
                registry.call("download_file", arguments)
            request.assert_not_called()
            self.assertFalse((self.root / "new.txt").exists())
            self.store.decide(waiting.exception.approval_id, True)
            registry.call("download_file", arguments)
        self.assertEqual((self.root / "new.txt").read_text(), "ok")

    def test_file_tools_cannot_access_approval_authority(self):
        tools = default_tools([self.root], allow_writes=True, config={"state_dir": str(self.root / "state")})
        for name, arguments in [
            ("read_file", {"path": "state/runtime.sqlite3"}),
            ("write_file", {"path": "state/forged", "content": "approved"}),
        ]:
            with self.assertRaises(PermissionError):
                tools.call(name, arguments)

    def test_activity_redacts_credentials_and_file_content(self):
        self.store.event("tool_start", {"password": "secret", "content": "private file", "url": "https://example.com"})
        event = self.store.events()[0]
        self.assertEqual(event["details"]["password"], "[redacted]")
        self.assertTrue(event["details"]["content"]["omitted"])
        self.assertEqual(event["details"]["url"], "https://example.com")

    def test_large_approval_payload_cannot_hide_unseen_action_tail(self):
        with self.assertRaises(ValueError):
            make_approver(self.store, "a")("desktop_type", {"text": "x" * 20001})
        self.assertEqual(self.store.approvals(), [])

    def test_hardlink_alias_cannot_read_or_change_private_state(self):
        authority = self.root / "state" / "secret.txt"
        authority.write_text("secret data")
        os.link(authority, self.root / "alias.txt")
        files = FileTools([self.root], protected_paths=[self.root / "state"])
        with self.assertRaises(PermissionError):
            files.read_file("alias.txt")
        with self.assertRaises(PermissionError):
            files.write_file("alias.txt", "changed")
        self.assertEqual(files.search_files(".", "secret")["matches"], [])

    def test_configuration_write_needs_approval_even_with_broad_home_grant(self):
        files = FileTools([self.root], make_approver(self.store, "a"), protected_paths=[self.root / "state"])
        with patch("local_llm_tools.files.Path.home", return_value=self.root):
            with self.assertRaises(ApprovalRequired):
                files.write_file(".bashrc", "echo hello\n")
        self.assertFalse((self.root / ".bashrc").exists())

    def test_binary_files_and_directory_pagination(self):
        files = FileTools([self.root], protected_paths=[self.root / "state"])
        data = bytes(range(256))
        files.write_bytes("binary.bin", base64.b64encode(data).decode())
        result = files.read_bytes("binary.bin", offset=200, length=100)
        self.assertEqual(base64.b64decode(result["base64"]), data[200:])
        self.assertTrue(result["eof"])
        first = files.list_files(limit=1)
        second = files.list_files(limit=1, offset=first["next_offset"])
        self.assertNotEqual(first["entries"][0]["name"], second["entries"][0]["name"])
