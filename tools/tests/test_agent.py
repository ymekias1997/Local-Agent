"""Exercise agent orchestration with real file tools and a scripted model."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from local_llm_tools.agent import Agent, OllamaClient
from local_llm_tools.__main__ import main
from local_llm_tools.registry import ToolRegistry


def tool_call(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"roots": [str(self.root)], "no_internet": True}

    def test_file_roundtrip_results_return_to_model(self):
        model = ScriptedModel(
            {"tool_calls": [tool_call("create_file", path="note.txt", content="hello\n")]},
            {"tool_calls": [tool_call("read_file", path="note.txt")]},
            {"content": "Created and read your note."},
        )
        events = []
        agent = Agent(self.config, client=model, on_event=events.append)
        self.assertEqual(agent.run("Create and read a note."), "Created and read your note.")
        self.assertEqual((self.root / "note.txt").read_text(), "hello\n")
        write_result = json.loads(model.requests[1]["messages"][-1]["content"])
        read_result = json.loads(model.requests[2]["messages"][-1]["content"])
        self.assertTrue(write_result["ok"])
        self.assertEqual(read_result["result"]["text"], "hello\n")
        self.assertEqual([event["name"] for event in events if event["event"] == "tool_end"],
                         ["create_file", "read_file"])

    def test_errors_return_to_model_without_ending_task(self):
        model = ScriptedModel(
            {"tool_calls": [tool_call("read_file", path="missing.txt"),
                            tool_call("unknown_tool")]},
            {"content": "The file was missing and the other tool is unavailable."},
        )
        agent = Agent(self.config, client=model)
        agent.run("Read the file.")
        results = [json.loads(message["content"]) for message in model.requests[1]["messages"]
                   if message["role"] == "tool"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result["ok"] for result in results))
        self.assertEqual(results[1]["error"], "KeyError")

    def test_conversation_is_preserved_across_tasks(self):
        model = ScriptedModel({"content": "First answer"}, {"content": "Second answer"})
        agent = Agent(self.config, client=model)
        agent.run("First task")
        agent.run("Second task")
        messages = model.requests[1]["messages"]
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant", "user"])
        self.assertEqual([item["content"] for item in messages[1:]],
                         ["First task", "First answer", "Second task"])

    def test_malformed_call_batch_does_not_execute_valid_prefix(self):
        for malformed in (None, {}, {"function": []}, {"function": {"name": 3}}):
            with self.subTest(malformed=malformed):
                model = ScriptedModel({"tool_calls": [tool_call("create_file", path="unwanted"), malformed]})
                with self.assertRaisesRegex(RuntimeError, "Malformed"):
                    Agent(self.config, client=model).run("Try this task")
                self.assertFalse((self.root / "unwanted").exists())

    def test_schema_errors_do_not_write(self):
        model = ScriptedModel(
            {"tool_calls": [tool_call("create_file", path="unwanted", content=["invalid"])]},
            {"content": "The tool rejected invalid arguments."},
        )
        Agent(self.config, client=model).run("Create a note")
        self.assertFalse((self.root / "unwanted").exists())
        self.assertFalse(json.loads(model.requests[1]["messages"][-1]["content"])["ok"])

    def test_model_step_limit(self):
        model = ScriptedModel({"tool_calls": [tool_call("list_files")]})
        with self.assertRaisesRegex(RuntimeError, "1 model steps"):
            Agent({**self.config, "max_steps": 1}, client=model).run("Keep going")
        self.assertEqual(len(model.requests), 1)

    def test_per_step_limit_prevents_all_actions(self):
        model = ScriptedModel({"tool_calls": [tool_call("create_file", path=str(i)) for i in range(9)]})
        with self.assertRaisesRegex(RuntimeError, "maximum 8"):
            Agent(self.config, client=model).run("Create files")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_total_call_limit_prevents_excess_actions(self):
        executed = []
        registry = ToolRegistry()
        registry.add(lambda: executed.append(True), "Track executions", {}, name="count")
        response = {"tool_calls": [tool_call("count") for _ in range(8)]}
        model = ScriptedModel(*(copy.deepcopy(response) for _ in range(9)))
        with self.assertRaisesRegex(RuntimeError, "64 tool-call limit"):
            Agent(self.config, client=model, registry=registry).run("Keep going")
        self.assertEqual(len(executed), 64)

    def test_read_only_mode_returns_disabled_tool_error(self):
        model = ScriptedModel(
            {"tool_calls": [tool_call("create_file", path="unwanted")]},
            {"content": "Write access is disabled."},
        )
        Agent({**self.config, "read_only": True}, client=model).run("Create a file")
        self.assertFalse((self.root / "unwanted").exists())
        self.assertNotIn("create_file", {tool["name"] for tool in model.requests[0]["tools"]})
        self.assertFalse(json.loads(model.requests[1]["messages"][-1]["content"])["ok"])

    def test_cli_direct_file_tool_and_read_only(self):
        arguments = ["local_llm_tools", "--root", str(self.root), "--state-dir", str(self.root / "state"), "--no-internet",
                     "--call", "create_file", "--arguments", '{"path":"from-cli","content":"saved"}']
        with patch("sys.argv", arguments), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())["bytes_written"], 5)
        self.assertEqual((self.root / "from-cli").read_text(), "saved")
        arguments[-1] = '{"path":"denied"}'
        with patch("sys.argv", arguments + ["--read-only"]), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(main(), 1)
            self.assertIn("not enabled", error.getvalue())
        self.assertFalse((self.root / "denied").exists())


class OllamaClientTests(unittest.TestCase):
    def test_request_uses_native_tools_and_nonstreaming_chat(self):
        messages = [{"role": "user", "content": "hello"}]
        definitions = [{"name": "get_time", "description": "Time", "parameters": {"type": "object"}}]
        with patch("local_llm_tools.agent.urlopen") as transport:
            transport.return_value.__enter__.return_value.read.return_value = b'{"message":{"role":"assistant","content":"hi"}}'
            answer = OllamaClient("my-model", "http://localhost:11434/", timeout=7).chat(messages, definitions)
        request = transport.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(payload, {"model": "my-model", "messages": messages, "stream": False,
                                   "tools": [{"type": "function", "function": definitions[0]}]})
        self.assertEqual(transport.call_args.kwargs["timeout"], 7)
        self.assertEqual(answer["content"], "hi")

    def test_connection_and_http_errors_are_actionable(self):
        errors = [(URLError("connection refused"), "Cannot reach Ollama"),
                  (HTTPError("http://localhost", 404, "missing", {}, io.BytesIO(b"model missing")), "Ollama HTTP 404")]
        for error, message in errors:
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            with self.subTest(error=error), patch("local_llm_tools.agent.urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, message):
                    OllamaClient("my-model").chat([], [])

    def test_rejects_missing_messages_and_oversized_responses(self):
        for body in (b"{}", b'{"message": []}', b"x" * 4_000_001):
            with self.subTest(size=len(body)), patch("local_llm_tools.agent.urlopen") as transport:
                transport.return_value.__enter__.return_value.read.return_value = body
                with self.assertRaises(RuntimeError):
                    OllamaClient("my-model").chat([], [])

    def test_invalid_model_endpoint_is_rejected(self):
        for url in ("file:///tmp/model", "http://user:secret@localhost:11434", "localhost:11434"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaClient("my-model", url)


if __name__ == "__main__":
    unittest.main()
