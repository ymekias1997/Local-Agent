"""Exercise child configuration and durable approvals across real worker restarts."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from local_llm_tools.memory import MemoryStore
from local_llm_tools.runtime import RuntimeStore
from local_llm_tools.sessions import SessionManager


class ChildAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'files'
        self.root.mkdir()
        self.state = Path(self.temp.name) / 'state'
        requests = self.requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                messages = body['messages']
                if messages[-1]['role'] == 'tool':
                    answer = {'content': 'Action finished'}
                elif messages[-1]['content'] == 'delete victim':
                    answer = {'tool_calls': [{'function': {'name': 'delete_file', 'arguments': {'path': 'victim'}}}]}
                else:
                    answer = {'content': 'Task finished'}
                data = json.dumps({'message': answer}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.manager = SessionManager(self.state, {'model': 'parent', 'agent_id': 'main', 'roots': [str(self.root)],
            'base_url': f'http://127.0.0.1:{self.server.server_port}', 'yes': True})

    def tearDown(self):
        for child in self.manager.list():
            self.manager.stop(child['id'])
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.03)
        self.fail('Worker did not reach expected state')

    def test_models_fresh_context_and_selected_memory_authority(self):
        memory = MemoryStore(self.state)
        own = memory.remember('main', 'Parent preference')['id']
        other = memory.remember('other', 'Private unrelated record')['id']
        with self.assertRaises(PermissionError):
            self.manager.start(memory_mode='selected', memory_ids=[other])
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.manager.start('password=do-not-save')
        with self.assertRaises(ValueError):
            self.manager.start(memory_mode='fresh', context='unexpected inheritance')
        child = self.manager.start('hello', model='different-model', enable_tools=False)['id']
        self.wait_for(lambda: any(m['role'] == 'assistant' for m in self.manager.read(child)['messages']))
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.manager.send(child, 'api_key=do-not-save')
        config = json.loads(self.manager._row(child)['config'])
        self.assertEqual(config['model'], 'different-model')
        self.assertFalse(config['yes'])
        self.assertEqual(config['memory_ids'], [])
        self.assertEqual(config['memory_scope'], 'private')
        self.assertEqual(self.requests[0]['tools'], [])
        self.assertEqual(self.requests[0]['model'], 'different-model')
        shared = self.manager.start(memory_mode='shared')['id']
        shared_config = json.loads(self.manager._row(shared)['config'])
        self.assertIn(own, shared_config['memory_ids'])
        self.assertNotIn(other, shared_config['memory_ids'])
        self.assertEqual([m['id'] for m in memory.search(shared_config['agent_id'], scope='shared', selected=shared_config['memory_ids'])], [own])
        learned = memory.remember(shared_config['agent_id'], 'Child lesson', shared=True)['id']
        self.assertIn(learned, {m['id'] for m in memory.search('main', scope='shared')})
        self.assertEqual(memory.search(config['agent_id']), [])

    def test_approval_wait_survives_stop_and_resume(self):
        (self.root / 'victim').write_text('remove only after approval')
        child = self.manager.start('delete victim')['id']
        self.manager.send(child, 'second task')
        self.wait_for(lambda: self.manager.status(child)['status'] == 'waiting_approval')
        messages = self.manager.read(child, limit=100)['messages']
        self.assertEqual(messages[0]['status'], 'waiting_approval')
        self.assertEqual(messages[1]['status'], 'queued')
        self.assertTrue((self.root / 'victim').exists())
        approval_id = messages[0]['approval_id']
        self.manager.stop(child)
        self.manager.resume(child)
        RuntimeStore(self.state).decide(approval_id, True)
        self.wait_for(lambda: any(m['role'] == 'assistant' for m in self.manager.read(child, limit=100)['messages']))
        self.assertFalse((self.root / 'victim').exists())
        self.manager.send(child, 'fresh task')
        self.wait_for(lambda: len([m for m in self.manager.read(child, limit=100)['messages'] if m['role'] == 'assistant']) == 2)
        self.assertEqual(len([r for r in self.requests if r['messages'][-1].get('content') == 'delete victim']), 1)

    def test_child_access_is_limited_to_its_parent(self):
        child = self.manager.start()['id']
        stranger = SessionManager(self.state, {'agent_id': 'stranger'})
        self.assertEqual(stranger.list(), [])
        with self.assertRaises(PermissionError):
            stranger.send(child, 'not authorized')


if __name__ == '__main__':
    unittest.main()
