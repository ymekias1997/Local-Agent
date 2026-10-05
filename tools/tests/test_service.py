"""Behavioral checks for durable recovery, scheduling and exact approval resume."""
import tempfile
import unittest
from pathlib import Path
from local_llm_tools.service import TaskStore, Scheduler
from local_llm_tools.runtime import RuntimeStore


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(Path(self.temp.name)/"state")
        self.runtime = RuntimeStore(Path(self.temp.name)/"state")
        self.config = {"model": "test", "roots": [self.temp.name]}

    def test_crash_requires_human_resume(self):
        task = self.store.add("work", self.config)
        self.store.claim()
        Scheduler(self.store, self.runtime)
        self.assertEqual(self.store.get(task['id'])['status'], 'paused')
        self.assertIsNone(self.store.claim())
        self.store.control(task['id'], 'resume')
        self.assertEqual(self.store.claim()['id'], task['id'])

    def test_schedule_coalesces_and_watcher_queues(self):
        schedule = self.store.add('repeat', self.config, interval=1)
        self.store.tick()
        self.store.update(schedule['id'], next_run=0)
        self.store.tick()
        self.assertEqual(len([t for t in self.store.list() if t['parent_id'] == schedule['id']]), 1)
        watched = Path(self.temp.name)/'watched'
        watched.mkdir()
        watch = self.store.add('react', self.config, watch_path=str(watched))
        self.store.tick()
        self.assertEqual(len([t for t in self.store.list() if t['parent_id'] == watch['id']]), 0)
        (watched/'new.txt').write_text('changed')
        self.store.tick()
        self.assertEqual(len([t for t in self.store.list() if t['parent_id'] == watch['id']]), 1)

    def test_approval_survives_scheduler_recreation(self):
        calls, instances = [], []
        class Agent:
            def __init__(self, config, approve, on_event):
                self.approve = approve
                instances.append(self)
            def run(self, prompt, resume=False):
                self.approve('download_file', {'url':'https://example.com/file','destination':'file'})
                calls.append(resume)
                return 'done'
        task = self.store.add('download', self.config)
        scheduler = Scheduler(self.store, self.runtime, agent_factory=Agent)
        scheduler.run_once()
        self.assertEqual(self.store.get(task['id'])['status'], 'waiting_approval')
        Scheduler(self.store, self.runtime, agent_factory=Agent).run_once()
        self.assertEqual(calls, [])
        approval = self.runtime.approvals()[0]
        self.runtime.decide(approval['id'], True)
        scheduler.run_once()
        self.assertEqual(calls, [True])
        self.assertEqual(len(instances), 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'completed')

    def test_watch_outside_granted_root_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add('watch', self.config, watch_path='/etc')

    def test_watch_keeps_changes_while_previous_task_waits(self):
        watched = Path(self.temp.name)/'busy-watch'
        watched.mkdir()
        template = self.store.add('react', self.config, watch_path=str(watched))
        (watched/'one').write_text('one')
        self.store.tick()
        first = next(t for t in self.store.list() if t['parent_id'] == template['id'])
        self.store.update(first['id'], status='waiting_approval')
        (watched/'two').write_text('two')
        self.store.tick()
        self.store.update(first['id'], status='completed')
        self.store.tick()
        children = [t for t in self.store.list() if t['parent_id'] == template['id']]
        self.assertEqual(len(children), 2)

    def test_recovery_resumes_only_safe_checkpoints(self):
        from local_llm_tools.memory import MemoryStore
        memory = MemoryStore(self.store.state_dir)
        for phase, expected in [('ready', 'queued'), ('executing', 'paused')]:
            task = self.store.add('work', self.config)
            self.store.update(task['id'], status='running')
            memory.save(task['agent_id'], {'phase':phase,'messages':[],'pending':[]})
            self.store.recover(self.runtime)
            self.assertEqual(self.store.get(task['id'])['status'], expected)
        task = self.store.add('sensitive work', self.config)
        self.store.update(task['id'], status='running')
        memory.save(task['agent_id'], {'phase':'pending_approval','messages':[]})
        request = self.runtime.request('download_file', {'url':'https://example.com'}, task['agent_id'], task['id'])
        self.store.recover(self.runtime)
        recovered = self.store.get(task['id'])
        self.assertEqual(recovered['status'], 'waiting_approval')
        self.assertEqual(recovered['approval_id'], request['id'])

    def test_credentials_rejected_or_redacted_before_storage(self):
        with self.assertRaises(ValueError):
            self.store.add('use password=hunter2', self.config)
        task = self.store.add('ordinary work', self.config)
        self.store.update(task['id'], result='password=hunter2', error='api_key=secret-value')
        saved = self.store.get(task['id'])
        self.assertNotIn('hunter2', saved['result'])
        self.assertNotIn('secret-value', saved['error'])

    def test_human_recovery_advances_without_replaying(self):
        from local_llm_tools.memory import MemoryStore
        from local_llm_tools.service import recover_task
        task = self.store.add('work', self.config)
        memory = MemoryStore(self.store.state_dir)
        memory.save(task['agent_id'], {'phase':'executing','messages':[], 'pending':[
            {'function':{'name':'delete_file','arguments':{'path':'example'}}}], 'cursor':0})
        self.store.update(task['id'], status='paused')
        result = recover_task(self.store, task['id'], 'The file is already absent')
        self.assertFalse(result['replayed'])
        checkpoint = memory.load(task['agent_id'])
        self.assertEqual(checkpoint['cursor'], 1)
        self.assertEqual(checkpoint['phase'], 'ready')
        self.assertEqual(self.store.get(task['id'])['status'], 'paused')
        scheduler = Scheduler(self.store, self.runtime)
        scheduler.agents[task['id']] = object()
        with self.assertRaises(ValueError):
            recover_task(self.store, task['id'], 'Do not race a live agent', scheduler)

    def test_cli_defaults_share_memories_and_accept_model_context_options(self):
        import contextlib
        import io
        from local_llm_tools.service import main
        from local_llm_tools.memory import MemoryStore
        with contextlib.redirect_stdout(io.StringIO()):
            main(['--state-dir', str(self.store.state_dir), 'add', '--model','test', '--vision', '--context-tokens','8192', 'first'])
        first = self.store.list()[0]
        self.assertTrue(first['config']['auto_memory'])
        self.assertTrue(first['config']['vision'])
        self.assertEqual(first['config']['context_tokens'], 8192)
        self.assertEqual(first['config']['memory_scope'], 'shared')
        memory = MemoryStore(self.store.state_dir)
        memory.remember(first['agent_id'], 'Use concise summaries', ['conversation:first'], shared=True)
        second = self.store.add('second', first['config'])
        found = memory.search(second['agent_id'], scope=second['config']['memory_scope'])
        self.assertEqual(found[0]['content'], 'Use concise summaries')
