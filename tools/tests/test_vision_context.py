"""Transport tests for screen evidence and bounded model context configuration."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_llm_tools.agent import Agent, OllamaClient
from local_llm_tools.registry import ToolRegistry


class VisionContextTests(unittest.TestCase):
    def test_image_references_are_translated_only_for_last_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            first, last = Path(directory) / "first.png", Path(directory) / "last.png"
            first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
            last.write_bytes(b"\x89PNG\r\n\x1a\nlast")
            client = OllamaClient("vision")
            client.context_tokens = 16384
            with patch("local_llm_tools.agent.urlopen") as transport:
                transport.return_value.__enter__.return_value.read.return_value = b'{"message":{"content":"seen"}}'
                client.chat([{"role": "user", "content": "before", "_image_path": str(first)},
                             {"role": "user", "content": "now", "_image_path": str(last)}], [])
            payload = json.loads(transport.call_args.args[0].data)
            self.assertNotIn("images", payload["messages"][0])
            self.assertNotIn("_image_path", payload["messages"][1])
            self.assertEqual(base64.b64decode(payload["messages"][1]["images"][0]), last.read_bytes())
            self.assertEqual(payload["options"]["num_ctx"], 16384)

    def test_context_estimate_reserves_tool_schema_and_answer_space(self):
        registry = ToolRegistry()
        registry.add(lambda: {}, "x" * 3000, {}, name="example")
        agent = Agent({"model": "test", "context_tokens": 8192}, registry=registry)
        self.assertLess(agent._context_budget(), 8192 * 2)
        self.assertGreater(agent._context_budget(), 4000)

    def test_arbitrary_non_image_files_are_never_sent_as_images(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "text"
            secret.write_text("not an image")
            with patch("local_llm_tools.agent.urlopen") as transport:
                with self.assertRaises(ValueError):
                    OllamaClient("vision").chat([{"role": "user", "content": "", "_image_path": str(secret)}], [])
                transport.assert_not_called()
