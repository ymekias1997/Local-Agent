"""Memory isolation and restart behavior are part of the runtime trust boundary."""
import tempfile
import unittest
from pathlib import Path

from local_llm_tools.memory import MemoryStore, redact
from local_llm_tools.agent import Agent
from local_llm_tools.registry import ToolRegistry


class Model:
    def __init__(self, *answers):
        self.answers = iter(answers)
    def chat(self, messages, tools):
        return next(self.answers)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(self.temp.name)

    def test_scope_and_owner_controls(self):
        private = self.store.remember('parent', 'private fact')['id']
        shared = self.store.remember('parent', 'shared fact', shared=True)['id']
        self.assertEqual(self.store.search('child'), [])
        self.assertEqual([x['id'] for x in self.store.search('child', scope='shared')], [shared])
        self.assertEqual([x['id'] for x in self.store.search('child', scope='selected', selected=[private])], [private])
        with self.assertRaises(PermissionError):
            self.store.update('child', private, 'changed')
        with self.assertRaises(PermissionError):
            self.store.forget('child', private)
        self.store.update('parent', private, 'corrected')
        self.store.forget('parent', private)
        self.assertEqual(self.store.search('parent', 'corrected'), [])

    def test_credentials_rejected_and_history_redacted(self):
        with self.assertRaises(ValueError):
            self.store.remember('main', 'api_key=supersecret')
        self.store.append('main', {'role': 'user', 'content': 'password=hunter2'})
        history = self.store.history('main')
        self.assertNotIn('hunter2', str(history))
        self.assertEqual(redact({'password': 'raw'}), {'password': '[REDACTED]'})
        self.assertNotIn('nested-secret', redact('{"password": "nested-secret"}'))

    def test_memory_deletion_cannot_remove_a_newer_revision(self):
        memory_id = self.store.remember('main', 'Original fact')['id']
        def approve(name, arguments):
            self.store.update('main', memory_id, 'Corrected while approval was pending')
            return True
        registry = ToolRegistry()
        self.store.register(registry, 'main', approve=approve)
        with self.assertRaisesRegex(PermissionError, 'changed'):
            registry.call('forget_memory', {'memory_id': memory_id})
        self.assertEqual(self.store.search('main')[0]['content'], 'Corrected while approval was pending')

    def test_compaction_preserves_original_history_across_restart(self):
        config = {'state_dir': self.temp.name, 'agent_id': 'main'}
        model = Model({'content': 'First answer'}, {'content': 'User wants a report; report still pending.'})
        agent = Agent(config, client=model, registry=ToolRegistry())
        agent.run('Please prepare a report')
        original = self.store.history('main')
        result = agent.compact()
        self.assertTrue(result['compacted'])
        self.assertTrue(result['sources'])
        self.assertEqual(original, self.store.history('main'))
        restored = Agent(config, client=Model(), registry=ToolRegistry())
        self.assertEqual(agent.messages, restored.messages)
        self.assertEqual(len(restored.messages), 2)
        self.assertIn('runtime approval', restored.messages[1]['content'])

    def test_unknown_external_effect_is_not_replayed(self):
        self.store.save('main', {'messages': [], 'phase': 'executing', 'pending': [], 'cursor': 0})
        agent = Agent({'state_dir': self.temp.name}, client=Model(), registry=ToolRegistry())
        with self.assertRaisesRegex(RuntimeError, 'unknown outcome'):
            agent.run('continue', resume=True)

    def test_approval_resume_never_repeats_completed_prefix(self):
        from local_llm_tools.runtime import ApprovalRequired, RuntimeStore, make_approver
        runtime = RuntimeStore(self.temp.name)
        approve = make_approver(runtime, 'main')
        effects = []
        registry = ToolRegistry()
        registry.add(lambda: effects.append('first'), 'First action', {}, name='first')
        def sensitive():
            approve('sensitive', {'path': '/example'})
            effects.append('second')
        registry.add(sensitive, 'Second action', {}, name='sensitive')
        calls = [{'function': {'name': name, 'arguments': {}}} for name in ['first', 'sensitive']]
        config = {'state_dir': self.temp.name}
        agent = Agent(config, client=Model({'tool_calls': calls}), registry=registry)
        with self.assertRaises(ApprovalRequired) as caught:
            agent.run('Perform both')
        self.assertEqual(effects, ['first'])
        runtime.decide(caught.exception.approval_id, True)
        restored = Agent(config, client=Model({'content': 'Both complete'}), registry=ToolRegistry())
        restored.tools.add(lambda: effects.append('first'), 'First', {}, name='first')
        restored.tools.add(sensitive, 'Second', {}, name='sensitive')
        self.assertEqual(restored.run('Perform both', resume=True), 'Both complete')
        self.assertEqual(effects, ['first', 'second'])
        self.assertEqual([m['role'] for m in restored.messages], ['system', 'user', 'assistant', 'tool', 'tool', 'assistant'])

    def test_model_without_tools_cannot_execute_returned_calls(self):
        registry = ToolRegistry()
        effects = []
        registry.add(lambda: effects.append(True), 'effect', {}, name='effect')
        model = Model({'tool_calls': [{'function': {'name': 'effect', 'arguments': {}}}]})
        agent = Agent({'enable_tools': False}, client=model, registry=registry)
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            agent.run('Answer only')
        self.assertEqual(effects, [])

    def test_pending_batch_blocks_compaction(self):
        agent = Agent({'state_dir': self.temp.name}, client=Model(), registry=ToolRegistry())
        agent.pending = [{'function': {'name': 'sensitive', 'arguments': {}}}]
        with self.assertRaisesRegex(RuntimeError, 'pending action'):
            agent.compact()

    def test_summary_failure_falls_back_without_losing_history(self):
        agent = Agent({'state_dir': self.temp.name}, client=Model({'content': 'done'}), registry=ToolRegistry())
        agent.run('Remember report status')
        previous = self.store.history('main')
        result = agent.compact()
        self.assertIn('summary unavailable', result['summary'])
        self.assertEqual(previous, self.store.history('main'))


if __name__ == '__main__':
    unittest.main()
