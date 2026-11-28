"""Synthetic planning and receipt tests; no Orca executable is used."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
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
        plan(self.args)
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
            ({}, report, 'completed', True), ({}, None, 'partial', True),
            ({'outcome': 'failed'}, None, 'failed', True),
            ({'stopReason': 'lost'}, report, 'blocked', True),
            ({'stopReason': 'exited'}, report, 'blocked', True),
            ({}, {'body': 'private'}, 'partial', True),
            ({'runtime_executed': False}, report, 'completed', False),
            ({}, dict(report, evidence=[]), 'completed', False)):
            with self.subTest(changes=changes, expected=expected, accepted=accepted):
                result = map_result(request, dict(receipt, **changes), registry, supplied)
                validate(result['result'], 'result')
                self.assertEqual(result['result']['status'], expected)
                self.assertEqual(result['accepted'], accepted)
        path = self.root / 'oversized.json'
        path.write_bytes(b' ' * 2_000_001)
        with self.assertRaises(ValueError):
            read_json(path)
