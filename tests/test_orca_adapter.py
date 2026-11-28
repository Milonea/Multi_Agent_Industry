"""Synthetic planning and receipt tests; no Orca executable is used."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_runtime.orca_dispatch import plan, load_registry
from agent_runtime.delegation_contract import validate
from adapters.orca.result import map_result, read_json


class OrcaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.overlay = self.root / 'overlay.json'
        self.project = {'owner_agent_id': 'implementer', 'status': 'active',
                        'constraints': ['Synthetic scope only'], 'validation': [['python3', '-V']],
                        'runtime': {'provider': 'orca', 'agent': 'codex', 'workspace': 'new-top-level'}}
        self.objective = self.root / 'objective.txt'
        self.objective.write_text('Inspect synthetic fixtures')
        self.args = argparse.Namespace(project='sample-project', role='implementer', agent=None,
            objective_file=str(self.objective), task_id='20261128-sample-task', output_dir=None, orca_repo_id=None)
        self.env = patch.dict(os.environ, {'MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY': str(self.overlay),
            'MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS': str(self.root / 'absent.json')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.save()

    def save(self):
        self.overlay.write_text(json.dumps({'schema_version': '1.0', 'scope': 'personal', 'projects': {'sample-project': self.project}}))
        self.overlay.chmod(0o600)

    def test_plan(self):
        output = plan(self.args)
        validate(output['request'], 'request')
        for text in ('Project: project:sample-project', 'Synthetic scope only', '["python3", "-V"]', 'Presentation policy', 'Placement: this machine (local)'):
            self.assertIn(text, output['spec'])
        for text in ('worktree_roots', 'repository', 'provenance', 'sha256', str(self.root)):
            self.assertNotIn(text, output['spec'] + json.dumps(output['argv']))

    def test_refusals_and_hold_agent(self):
        for status in ('hold', 'inventory_only'):
            self.project['status'] = status
            if status == 'hold':
                self.project['hold_agent_id'] = 'researcher'
            else:
                self.project.pop('hold_agent_id', None)
            self.save()
            with self.assertRaises(ValueError):
                plan(self.args)
            if status == 'hold':
                self.args.role, self.args.agent = 'researcher', 'researcher'
                self.assertEqual(plan(self.args)['request']['agent_id'], 'researcher')
                self.args.role, self.args.agent = 'implementer', None
        self.project['status'] = 'active'
        self.project.pop('runtime')
        self.save()
        with self.assertRaises(ValueError):
            plan(self.args)

    def test_private_outputs(self):
        self.args.output_dir = str(Path.cwd() / 'sample-output')
        with self.assertRaises(ValueError):
            plan(self.args)
        self.args.output_dir = str(self.root / 'output')
        output = plan(self.args)
        self.assertEqual(json.loads((self.root / 'output/dispatch-plan.json').read_text()), output)
        self.assertEqual((self.root / 'output').stat().st_mode & 0o777, 0o700)
        for path in (self.root / 'output').iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mapping(self):
        request = plan(self.args)['request']
        registry = load_registry()
        receipt = {'taskId': 'sample-orca-task', 'dispatchId': 'sample-dispatch', 'outcome': 'succeeded', 'runtime_executed': True}
        base = map_result(request, receipt, registry)['result']
        report = deepcopy(base)
        report.update(status='completed', evidence=['Synthetic check passed'])
        for changes, supplied, expected, accepted in (
            ({}, report, 'completed', True), ({}, None, 'partial', False),
            ({'outcome': 'failed'}, None, 'failed', False),
            ({'stopReason': 'lost'}, report, 'blocked', True),
            ({'stopReason': 'exited'}, report, 'blocked', True),
            ({'stopReason': 'unverifiable'}, report, 'blocked', True),
            ({}, {'body': 'private'}, 'partial', False),
            ({'runtime_executed': False}, report, 'completed', False),
            ({'runtime_executed': 'true'}, report, 'completed', False),
            ({}, dict(report, task_id='20261128-sample-other'), 'partial', False),
            ({}, dict(report, agent_id='reviewer'), 'partial', False),
            ({}, dict(report, evidence=[]), 'partial', False),
            ({}, dict(report, evidence=['orca:dispatch:sample-dispatch']), 'partial', False)):
            with self.subTest(changes=changes, expected=expected, accepted=accepted):
                result = map_result(request, dict(receipt, **changes), registry, supplied)
                validate(result['result'], 'result')
                self.assertEqual(result['result']['status'], expected)
                self.assertEqual(result['accepted'], accepted)
        path = self.root / 'oversized.json'
        path.write_text(json.dumps('x' * 1_999_999))
        self.assertEqual(path.stat().st_size, 2_000_001)
        with self.assertRaises(ValueError):
            read_json(path)

    def test_worker_header_routing(self):
        from adapters.codex.route_agent import handle
        self.args.role = 'reviewer'
        for hold in (False, True):
            if hold:
                self.project.update(status='hold', hold_agent_id='reviewer')
                self.save()
            output = plan(self.args)
            header = output['spec'].split('\n\n')[0]
            with patch('adapters.codex.route_agent.tempfile.gettempdir', return_value=str(self.root)):
                event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'sample-session', 'cwd': str(Path.cwd()), 'prompt': header}
                context = handle(event)['hookSpecificOutput']['additionalContext']
                self.assertIn('active_agent_id=' + output['request']['agent_id'], context)
                if hold:
                    event['prompt'] = header.replace('@agent:reviewer', '@agent:implementer')
                    self.assertEqual(handle(event)['decision'], 'block')

    def test_guard_boundaries(self):
        self.args.orca_repo_id = 'sample\ninvalid'
        with self.assertRaisesRegex(ValueError, 'repo identifier'):
            plan(self.args)
        self.args.orca_repo_id = None
        self.args.agent = 'reviewer'
        with self.assertRaisesRegex(ValueError, 'requested role'):
            plan(self.args)
        self.args.agent = None
        self.objective.write_text('x' * 2_000_001)
        with self.assertRaisesRegex(ValueError, 'objective exceeds'):
            plan(self.args)
        self.objective.write_text('Synthetic objective')
        self.project.update(path=str(self.root / 'sample-project'),
                            repository='sample-owner/sample-repository',
                            worktree_roots=[str(self.root / 'sample-worktrees')])
        for private in (self.project['path'], self.project['repository'], self.project['worktree_roots'][0]):
            self.project['constraints'] = [private]
            self.save()
            with self.assertRaisesRegex(ValueError, 'private project location'):
                plan(self.args)

    def test_style_only(self):
        personal = self.root / 'absent.json'
        personal.write_text(json.dumps({'schema_version': '1.0', 'scope': 'personal',
            'profiles': {'sample-profile': {'profile_id': 'sample-profile', 'locale': 'en',
                'style': 'Synthetic concise style', 'tone_tags': [],
                'identity_data': {'display_name': 'sample-private-identity'},
                'source': {'project': 'sample-private-source', 'records': [
                    {'path': 'sample-private-record.json', 'record_id': 'sample-record', 'sha256': 'a' * 64}]}}},
            'default_profile_id': 'sample-profile', 'assignments': {'agents': {}, 'roles': {}}}))
        personal.chmod(0o600)
        spec = plan(self.args)['spec']
        self.assertIn('Synthetic concise style', spec)
        self.assertNotIn('sample-private-identity', spec)
        self.assertNotIn('identity_data', spec)
        self.assertNotIn('sample-private-source', spec)
        self.assertNotIn('sample-private-record', spec)

    def test_cli_malformed_inputs_are_redacted(self):
        request = self.root / 'request.json'
        receipt = self.root / 'receipt.json'
        report = self.root / 'report.json'
        good_request = plan(self.args)['request']
        good_receipt = {'taskId': 'sample-task', 'dispatchId': 'sample-dispatch', 'outcome': 'succeeded'}
        command = [sys.executable, '-m', 'agent_runtime', 'ingest-orca-result',
                   '--request', str(request), '--worker-done', str(receipt), '--report', str(report)]
        for bad_field in ('stopReason', 'outcome', 'request-depth', 'receipt-depth', 'report-depth'):
            request.write_text(json.dumps(good_request))
            receipt.write_text(json.dumps(good_receipt))
            report.write_text('{}')
            if bad_field.endswith('-depth'):
                {'request-depth': request, 'receipt-depth': receipt, 'report-depth': report}[bad_field].write_text('[' * 10000)
            else:
                receipt.write_text(json.dumps(dict(good_receipt, **{bad_field: []})))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotIn('Traceback', result.stderr)
            self.assertNotIn(str(self.root), result.stderr)
            self.assertNotIn(str(Path.cwd()), result.stderr)
            if bad_field != 'report-depth':
                self.assertNotEqual(result.returncode, 0)
