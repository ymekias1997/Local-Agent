"""Exercise real HTTP authorization, origin enforcement and shared task controls."""
import json
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from local_llm_tools.dashboard import make_server, auth_token
from local_llm_tools.service import TaskStore, Scheduler
from local_llm_tools.runtime import RuntimeStore


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(self.temp.name)
        self.runtime = RuntimeStore(self.temp.name)
        self.scheduler = Scheduler(self.store, self.runtime, {'model':'test','roots':[]})
        self.server = make_server(self.store, self.runtime, self.scheduler, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = auth_token(self.temp.name)

    def request(self, path, data=None, authorized=True, **headers):
        if authorized:
            headers['Authorization'] = 'Bearer ' + self.token
        headers['Content-Type'] = 'application/json'
        return urlopen(Request(self.url+path, data=json.dumps(data).encode() if data is not None else None, headers=headers), timeout=3)

    def test_private_api_and_public_login(self):
        with self.request('/', authorized=False) as reply:
            self.assertIn(b'Local Agent', reply.read())
        with self.assertRaises(HTTPError) as ctx:
            self.request('/api/tasks', authorized=False)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()
        with self.request('/api/tasks', {'prompt':'hello','config':{'roots':['/']}}) as reply:
            task = json.load(reply)
        self.assertEqual(task['config']['roots'], [])
        with self.request('/api/control', {'id':task['id'],'action':'pause'}) as reply:
            self.assertEqual(json.load(reply)['status'], 'paused')

    def test_cross_site_and_rebinding_rejected(self):
        for headers in ({'Origin':'https://evil.example'}, {'Host':'evil.example'}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as ctx:
                self.request('/api/tasks', **headers)
            self.assertEqual(ctx.exception.code, 403)
            ctx.exception.close()
