"""Real namespace integration checks; Linux must permit unprivileged namespaces."""
from pathlib import Path
import shutil
import tempfile
import unittest

from local_llm_tools.files import FileTools
from local_llm_tools.sandbox import ScriptSandbox


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("prlimit"), "bubblewrap/prlimit unavailable")
class SandboxIntegrationTests(unittest.TestCase):
    def test_scripts_cannot_write_inputs_or_reach_network(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "input"
            root.mkdir()
            (root / "data").write_text("original")
            sandbox = ScriptSandbox(base / "state", FileTools([root]))
            result = sandbox.run_code("python", '''
from pathlib import Path
import socket
assert Path('/inputs/root0/data').read_text() == 'original'
try:
    Path('/inputs/root0/data').write_text('changed')
    raise AssertionError('Input mount is writable')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
    raise AssertionError('Network reachable')
except OSError:
    pass
assert not Path('/run/user').exists()
Path('output').write_text('generated')
print('isolation verified')
''')
            if result["returncode"] and "bwrap:" in result["stderr"]:
                self.skipTest("Host sandbox disallows bubblewrap namespaces: " + result["stderr"].strip())
            self.assertEqual(result["returncode"], 0, result)
            self.assertIn("isolation verified", result["stdout"])
            self.assertEqual((root / "data").read_text(), "original")
            sandbox.commit_sandbox_file(result["run_id"], "output", str(root / "result"))
            self.assertEqual((root / "result").read_text(), "generated")


if __name__ == '__main__':
    unittest.main()
