"""Map explicitly supplied Orca receipts without trusting completion claims."""
from copy import deepcopy
import json
from pathlib import Path

from agent_runtime.delegation_contract import validate
from agent_runtime.handoff import accept_handoff_result


def read_json(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('input exceeds size limit')
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('input could not be read as JSON') from exc


def map_result(request, receipt, registry, report=None):
    validate(request, 'request')
    if not isinstance(receipt, dict) or not all(isinstance(receipt.get(k), str) and receipt[k].strip() for k in ('taskId', 'dispatchId')):
        raise ValueError('invalid Orca receipt identity')
    stopped = receipt.get('stopReason') in {'lost', 'exited', 'unverifiable'}
    files = receipt.get('filesModified', [])
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError('invalid Orca changed files')
    if not stopped and receipt.get('outcome') not in {'succeeded', 'failed'}:
        raise ValueError('invalid Orca outcome')
    valid_report = False
    try:
        validate(report, 'result')
        valid_report = all(report[k] == request[k] for k in ('task_id', 'route_id', 'agent_id', 'runtime_agent', 'project_root', 'scope'))
    except ValueError:
        pass
    result = deepcopy(report) if valid_report else {
        **{k: deepcopy(request[k]) for k in ('contract_version', 'task_id', 'route_id', 'agent_id', 'runtime_agent', 'project_root', 'scope')},
        'status': 'partial' if receipt.get('outcome') == 'succeeded' else 'failed',
        'summary': 'Orca receipt ingested; report missing or invalid.',
        'changed_files': files, 'commands_run': [],
        'validation': {'passed': [], 'failed': [], 'not_run': request['required_validation']},
        'risks_or_side_effects': [], 'remaining_work': ['Coordinator must verify worker evidence.'], 'evidence': [],
    }
    unsupported_completion = result['status'] == 'completed' and not any(e.strip() for e in result['evidence'])
    if stopped:
        result['status'] = 'blocked'
    elif receipt.get('outcome') == 'failed':
        result['status'] = 'failed'
    result['evidence'].append('orca:dispatch:' + receipt['dispatchId'])
    validate(result, 'result')
    handoff = accept_handoff_result(registry, {
        'agent_id': result['agent_id'], 'status': 'blocked' if result['status'] == 'holding' else result['status'],
        'runtime_executed': receipt.get('runtime_executed') is True,
        'summary': result['summary'], 'evidence': [] if unsupported_completion else result['evidence'],
    })
    return {'result': result, 'runtime_executed': receipt.get('runtime_executed') is True,
            'accepted': handoff.reported_agent_id is not None, 'handoff_status': handoff.status,
            'reason': handoff.reason, 'valid_report': valid_report}
