"""File operations and schema enforcement using isolated temporary directories."""
import tempfile
import unittest
from pathlib import Path

from local_llm_tools.files import FileTools, MAX_FILE_BYTES
from local_llm_tools.registry import ToolRegistry, integer, string


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "allowed"
        self.root.mkdir()
        self.files = FileTools([self.root])
        self.registry = ToolRegistry()
        self.files.register(self.registry, writes=True)

    def call(self, name, **arguments):
        return self.registry.call(name, arguments)

    def test_create_read_append_overwrite_and_search(self):
        self.call("make_directory", path="notes/sub")
        self.call("create_file", path="notes/sub/a.txt", content="one\nTwo\n")
        self.call("append_file", path="notes/sub/a.txt", content="three\n")
        result = self.call("read_file", path="notes/sub/a.txt", start_line=2, max_lines=1)
        self.assertEqual(result["text"], "Two\n")
        self.assertTrue(result["truncated"])
        match = self.call("search_files", path="notes", query="two")["matches"][0]
        self.assertEqual(match["line"], 2)
        self.call("write_file", path="notes/sub/a.txt", content="replaced")
        self.assertEqual(self.call("read_file", path="notes/sub/a.txt")["text"], "replaced")
        self.assertEqual(self.call("list_files", path="notes/sub")["entries"], [{"name": "a.txt", "kind": "file"}])

    def test_create_is_exclusive_and_modes_work(self):
        self.call("create_file", path="a", content="original")
        with self.assertRaises(FileExistsError):
            self.call("create_file", path="a", content="lost")
        self.assertEqual((self.root / "a").read_text(), "original")
        self.call("write_text", path="a", content="!", mode="append")
        self.assertEqual((self.root / "a").read_text(), "original!")
        with self.assertRaises(ValueError):
            self.files.save_bytes("b", b"x", "invalid")
        self.assertFalse((self.root / "b").exists())

    def test_read_only_registry_omits_mutations(self):
        registry = ToolRegistry()
        self.files.register(registry, writes=False)
        names = {definition["name"] for definition in registry.definitions()}
        self.assertEqual(names, {"list_files", "file_info", "read_bytes", "read_file", "search_files", "search_text"})
        with self.assertRaises(KeyError):
            registry.call("write_file", {"path": "a", "content": "x"})

    def test_delete_requires_approval(self):
        self.call("create_file", path="a")
        with self.assertRaises(PermissionError):
            self.call("delete_file", path="a")
        approved = []
        files = FileTools([self.root], lambda name, args: approved.append((name, args)) or True)
        self.assertTrue(files.delete_file("a")["deleted"])
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0][0], "delete_file")
        self.assertEqual(approved[0][1]["path"], str(self.root / "a"))
        self.assertEqual(approved[0][1]["bytes"], 0)
        self.assertIn("modified_ns", approved[0][1])
        self.assertIn("inode", approved[0][1])
        self.assertFalse((self.root / "a").exists())
        self.call("make_directory", path="folder")
        with self.assertRaises(ValueError):
            files.delete_file("folder")

    def test_delete_rechecks_after_approval(self):
        self.call("create_file", path="a", content="old")
        def approve(name, arguments):
            (self.root / "a").write_text("changed file")
            return True
        with self.assertRaises(PermissionError):
            FileTools([self.root], approve).delete_file("a")
        self.assertEqual((self.root / "a").read_text(), "changed file")

    def test_escape_and_symlinks(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside")
        (self.root / "escape").symlink_to(self.base, target_is_directory=True)
        for path in ("../outside.txt", str(outside), "escape/outside.txt"):
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):
                    self.files.read_file(path)
                with self.assertRaises(PermissionError):
                    self.files.write_file(path, "changed")
        self.files.create_file("a", "inside")
        (self.root / "link").symlink_to(self.root / "a")
        self.assertEqual(self.files.read_file("link")["text"], "inside")
        with self.assertRaises(PermissionError):
            self.files.write_file("link", "changed")
        with self.assertRaises(PermissionError):
            self.files.make_directory(".")
        self.assertEqual(outside.read_text(), "outside")

    def test_utf8_byte_limit_and_pagination(self):
        with self.assertRaises(ValueError):
            self.files.create_file("large", "é" * (MAX_FILE_BYTES // 2 + 1))
        self.assertFalse((self.root / "large").exists())
        self.files.create_file("lines", "a\nb\nc\n")
        self.assertEqual(self.files.read_file("lines", 3)["text"], "c\n")
        self.assertFalse(self.files.read_file("lines", 3)["truncated"])
        with self.assertRaises(ValueError):
            self.files.read_file("lines", 0)
        with self.assertRaises(ValueError):
            self.files.list_files(limit=-1)

    def test_search_truncation_and_binary_files(self):
        self.files.create_file("a", "hello\nhello\n")
        (self.root / "binary").write_bytes(b"hello\x00")
        self.assertFalse(self.files.search_files(".", "hello", 2)["truncated"])
        result = self.files.search_files(".", "hello", 1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertTrue(result["truncated"])


class RegistryTests(unittest.TestCase):
    def test_validation_precedes_side_effects(self):
        calls = []
        registry = ToolRegistry()
        registry.add(lambda count, label: calls.append(count), "Example", {
            "count": integer(1, 3, 1), "label": string(minLength=1, maxLength=3),
        }, ["count", "label"], name="example")
        for arguments in ({"count": True, "label": "x"}, {"count": 4, "label": "x"},
                          {"count": 2, "label": "long"}, {"count": 2},
                          {"count": 2, "label": "x", "extra": 1}, "[]"):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    registry.call("example", arguments)
        self.assertEqual(calls, [])
        registry.call("example", '{"count": 2, "label": "x"}')
        self.assertEqual(calls, [2])
        definitions = registry.definitions()
        definitions[0]["parameters"]["properties"]["count"]["maximum"] = 100
        with self.assertRaises(ValueError):
            registry.call("example", {"count": 4, "label": "x"})


if __name__ == "__main__":
    unittest.main()
