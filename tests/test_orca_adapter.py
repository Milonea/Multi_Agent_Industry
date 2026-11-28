"""Synthetic planning and receipt tests; no Orca executable is used."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import shlex
import unittest
from unittest.mock import patch

from agent_runtime.orca_dispatch import plan, load_registry
from agent_runtime.delegation_contract import REPORT_PLACEHOLDER, validate
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

    def test_report_contract_example_and_local_check(self):
        schema = json.loads((Path.cwd() / '.agents/contracts/delegation-result.schema.json').read_text())
        output = plan(self.args)
        spec = output['spec']
        report_section = spec.split('\n\nReport\n', 1)[1]
        self.assertIn('overrides any repo-local report format', report_section)
        self.assertIn('Unknown top-level fields are forbidden (additionalProperties: false)', report_section)
        self.assertIn('Optional schema fields may be omitted', report_section)
        self.assertIn('ignored scratch path, or in a temp directory', report_section)
        self.assertIn('Before worker_done', report_section)
        self.assertIn('--report-path', report_section)
        required_text = report_section.split('required top-level fields: ', 1)[1].split('.\n', 1)[0]
        self.assertEqual(json.loads(required_text), schema['required'])
        allowed_text = report_section.split('Allowed top-level fields: ', 1)[1].split('.\n', 1)[0]
        self.assertEqual(json.loads(allowed_text), list(schema['properties']))
        self.assertNotIn('Request identity and task context:', report_section)
        self.assertIn('Existing changes: ' + output['request']['existing_changes'], report_section)
        self.assertIn(json.dumps(output['request']['task_retry']), report_section)
        status_text = report_section.split('Allowed status values: ', 1)[1].split('. Select', 1)[0]
        self.assertEqual(json.loads(status_text), schema['properties']['status']['enum'])
        example = json.loads(report_section.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(set(example), set(schema['required']))
        self.assertEqual(example['status'], 'partial')
        for key in ('contract_version', 'task_id', 'route_id', 'agent_id', 'runtime_agent', 'project_root', 'scope'):
            self.assertEqual(example[key], output['request'][key])
        validate(example, 'result')
        receipt = {'taskId': 'sample-task', 'dispatchId': 'sample-dispatch', 'outcome': 'succeeded', 'runtime_executed': True}
        for unfilled in (example, dict(example, status='completed')):
            result = map_result(output['request'], receipt, load_registry(), unfilled)
            self.assertFalse(result['valid_report'])
            self.assertFalse(result['accepted'])
            self.assertEqual(result['result']['status'], 'partial')
        self.assertEqual(example['summary'], REPORT_PLACEHOLDER)
        self.assertEqual(example['evidence'], [REPORT_PLACEHOLDER])
        self.assertEqual(example['validation']['passed'], [REPORT_PLACEHOLDER])
        example.update(status='completed', summary='Synthetic validation passed', changed_files=[],
            commands_run=['python3 -V'], validation={'passed': ['Synthetic validation'], 'failed': [], 'not_run': []},
            risks_or_side_effects=[], remaining_work=[], evidence=['Synthetic command output'])
        validate(example, 'result')
        result = map_result(output['request'], receipt, load_registry(), example)
        self.assertTrue(result['valid_report'])
        self.assertTrue(result['accepted'])
        self.assertEqual(result['result']['status'], 'completed')
        command = shlex.split(report_section.split('```sh\n', 1)[1].split('\n```', 1)[0])
        report_file = self.root / 'sample-report.json'
        command[-1] = str(report_file)
        report_file.write_text(json.dumps(example))
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        request_file, receipt_file = self.root / 'sample-request.json', self.root / 'sample-receipt.json'
        request_file.write_text(json.dumps(output['request']))
        receipt_file.write_text(json.dumps(receipt))
        ingested = subprocess.run([sys.executable, '-m', 'agent_runtime', 'ingest-orca-result',
            '--request', str(request_file), '--worker-done', str(receipt_file), '--report', str(report_file)],
            capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(ingested.stdout)['accepted'])
        optional = dict(example, task_retry=output['request']['task_retry'])
        validate(optional, 'result')
        self.assertTrue(map_result(output['request'], receipt, load_registry(), optional)['accepted'])
        missing = dict(example)
        del missing['agent_id']
        for optimize in ('', '1'):
            env = dict(os.environ, PYTHONOPTIMIZE=optimize)
            report_file.write_text(json.dumps(optional))
            self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            for invalid, diagnostic in ((missing, 'agent_id'), (dict(example, student_id='sample-extra'), 'student_id'), ([], 'missing=')):
                report_file.write_text(json.dumps(invalid))
                checked = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertNotEqual(checked.returncode, 0)
                self.assertIn('missing=', checked.stderr)
                self.assertIn('extra=', checked.stderr)
                self.assertIn(diagnostic, checked.stderr)
        for marker_report in (dict(example, summary=REPORT_PLACEHOLDER),
                              dict(optional, task_retry=dict(optional['task_retry'], hold_reason='prefix ' + REPORT_PLACEHOLDER))):
            result = map_result(output['request'], receipt, load_registry(), marker_report)
            self.assertFalse(result['valid_report'])
            self.assertFalse(result['accepted'])
        extra = dict(example, student_id='sample-legacy-id')
        with self.assertRaises(ValueError):
            validate(extra, 'result')
        result = map_result(output['request'], receipt, load_registry(), extra)
        self.assertFalse(result['valid_report'])
        self.assertFalse(result['accepted'])
        report_file.write_text(json.dumps(extra))
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_report_contract_is_derived_at_runtime(self):
        from agent_runtime.orca_dispatch import _report_section
        schema = json.loads((Path.cwd() / '.agents/contracts/delegation-result.schema.json').read_text())
        schema['required'].append('sample_new_field')
        schema['properties']['sample_new_field'] = {}
        schema['properties']['status']['enum'].append('sample_status')
        path = self.root / '.agents/contracts/delegation-result.schema.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(schema))
        request = plan(self.args)['request']
        with patch('agent_runtime.orca_dispatch.ROOT', self.root):
            section = _report_section(request)
        fields = json.loads(section.split('required top-level fields: ', 1)[1].split('.\n', 1)[0])
        self.assertEqual(fields, schema['required'])
        self.assertIn('sample_status', section.split('Allowed status values: ', 1)[1].split('. Select', 1)[0])
        example = json.loads(section.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(set(example), set(fields))

    def test_manual_gates_are_separate(self):
        self.project['manual_gates'] = ['sample-manual-approval']
        self.save()
        output = plan(self.args)
        spec = output['spec']
        manual = spec.split('Manual gates (never clear these yourself)\n', 1)[1].split('\n\n', 1)[0]
        self.assertEqual(manual, 'sample-manual-approval')
        self.assertEqual(spec.count('sample-manual-approval'), 1)
        self.assertNotIn('sample-manual-approval', json.dumps(output['request']))

    def test_null_escalation_outcome(self):
        request = plan(self.args)['request']
        registry = load_registry()
        for reason in ('lost', 'exited', 'unverifiable'):
            with self.subTest(reason=reason):
                receipt = {'taskId': 'sample-task', 'dispatchId': 'sample-dispatch',
                           'stopReason': reason, 'outcome': None}
                self.assertEqual(map_result(request, receipt, registry)['result']['status'], 'blocked')
                for invalid in ([], {}, 1, False):
                    with self.assertRaises(ValueError):
                        map_result(request, dict(receipt, outcome=invalid), registry)

    def test_oversized_integer_error_is_generic(self):
        # Set a limit explicitly so this test also works when the host disables it.
        payload = self.root / 'integer.json'
        text = '9' * 5000
        payload.write_text(text)
        command = [sys.executable, '-c',
            'import sys; from agent_runtime.cli import main; '
            'sys.set_int_max_str_digits(4300); '
            'sys.exit(main(sys.argv[1:]))',
            'ingest-orca-result', '--request', str(payload), '--worker-done', str(payload)]
        if not hasattr(sys, 'set_int_max_str_digits'):
            self.skipTest('host Python has no integer conversion limit')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), 'Command refused: input could not be read as JSON')
        self.assertNotIn(str(self.root), result.stderr)
        self.assertNotIn(text, result.stderr)

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
