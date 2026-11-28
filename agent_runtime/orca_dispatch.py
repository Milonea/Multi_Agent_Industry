"""Produce reviewable local Orca plans; never invoke Orca."""
import json
import os
import re
import shlex
from pathlib import Path

from .delegation_contract import ROOT, REPORT_PLACEHOLDER, validate
from .persona import presentation_context, presentation_policy_context
from .persona_layers import load_persona_layers
from .private_files import create_private_json
from .project_overlay import load_project_overlay, apply_project_overlay
from .registry import AgentRegistry
from .routing import resolve_route, IMPLEMENTATION_PERMISSIONS
from portfolio_control_plane.retry import TaskRetryState

PROHIBITIONS = ['No external writes, deployment, paid services or destructive Git operations.',
                'Preserve unrelated changes and private data; do not launch other workers.']


def load_registry():
    path = Path(os.environ.get('MULTI_AGENT_INDUSTRY_REGISTRY', ROOT / '.agents/registry.json'))
    return AgentRegistry.from_dict(json.loads(path.read_text(encoding='utf-8')))


def _report_section(request):
    """Embed the current result contract, not a destination repo's report dialect."""
    schema = json.loads((ROOT / '.agents/contracts/delegation-result.schema.json').read_text(encoding='utf-8'))
    required = schema['required']
    allowed = list(schema['properties'])
    statuses = schema['properties']['status']['enum']

    def placeholder(rule):
        if 'const' in rule:
            return rule['const']
        if 'enum' in rule:
            return rule['enum'][0]
        if rule.get('type') == 'object':
            return {key: placeholder(rule['properties'][key]) for key in rule['required']}
        if rule.get('type') == 'array':
            return [REPORT_PLACEHOLDER]
        return REPORT_PLACEHOLDER

    identity = {'contract_version', 'task_id', 'route_id', 'agent_id', 'runtime_agent', 'project_root', 'scope'}
    example = {key: request[key] if key in identity else placeholder(schema['properties'][key]) for key in required}
    # A template is not a completion claim. Workers must select their actual outcome.
    example['status'] = 'partial'
    check = (
        'import json,sys; '
        'report=json.load(open(sys.argv[1],encoding="utf-8")); '
        f'required={json.dumps(required)}; '
        f'allowed={json.dumps(allowed)}; '
        'keys=set(report) if isinstance(report,dict) else set(); '
        'missing=sorted(set(required)-keys); extra=sorted(keys-set(allowed)); '
        'sys.exit(0 if isinstance(report,dict) and not missing and not extra else '
        '"report keys invalid: missing="+str(missing)+", extra="+str(extra))'
    )
    return '\n'.join([
        'Report',
        'The MAI delegation-result contract overrides any repo-local report format for this handoff.',
        'Use these required top-level fields: ' + json.dumps(required) + '.',
        'Allowed top-level fields: ' + json.dumps(allowed) + '.',
        'Optional schema fields may be omitted. Unknown top-level fields are forbidden (additionalProperties: false). Do not rename identity fields.',
        'Allowed status values: ' + json.dumps(statuses) + '. Select the actual outcome; never invent evidence.',
        'Replace every ' + REPORT_PLACEHOLDER + ' marker with observed values of the same type; use empty arrays when appropriate. Reports retaining the marker anywhere are invalid.',
        'Example delegation-result JSON:', '```json', json.dumps(example, ensure_ascii=False, indent=2), '```',
        'Write the report inside your worktree under an ignored scratch path, or in a temp directory. Do not commit it.',
        'Before worker_done, run this stdlib-only key check (replace the final argument with your report file):',
        '```sh', shlex.join(['python3', '-c', check, '<report.json>']), '```',
        'This checks top-level keys only; also verify types, identity, status, validation and work evidence against the example.',
        'Pass the checked report file with Orca worker_done --report-path.',
        'Existing changes: ' + request['existing_changes'],
        'Task retry context (do not copy into the report): ' + json.dumps(request['task_retry']),
    ])


def plan(args):
    if args.orca_repo_id is not None and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.orca_repo_id) is None:
        raise ValueError('invalid Orca repo identifier')
    registry = load_registry()
    overlay = load_project_overlay(registry, repo_root=ROOT)
    registry = apply_project_overlay(registry, overlay, base_dir=ROOT)
    project = overlay.projects.get(args.project)
    if project is None or project.runtime is None or project.runtime.provider != 'orca':
        raise ValueError('project requires an Orca runtime block')
    decision = resolve_route(registry, project_id=args.project, requested_role=args.role, explicit_agent_id=args.agent)
    if not decision.allowed:
        raise ValueError('project rules refuse dispatch')
    agent = registry.get(decision.state.active_agent_id)
    if agent.role != args.role:
        raise ValueError('explicit agent does not match requested role')
    layers = load_persona_layers(registry, global_path=ROOT / '.agents/persona-settings.json', repo_root=ROOT, project_overlay=overlay)
    selected = layers.resolve(agent, project_id=args.project)
    # Only the public style fields enter the spec; identity/source metadata stays private.
    profile = selected.profile if selected else agent.persona
    style = {k: profile[k] for k in ('profile_id', 'locale', 'style', 'tone_tags')} if profile else None
    with Path(args.objective_file).open('rb') as stream:
        objective = stream.read(2_000_001)
    if len(objective) > 2_000_000:
        raise ValueError('objective exceeds size limit')
    objective = objective.decode('utf-8').strip()
    request = dict(contract_version='2.0', task_id=args.task_id, **{'from': 'root-orchestrator'},
                   route_id=args.project, agent_id=agent.agent_id, runtime_agent=project.runtime.agent,
                   project_root='.', objective=objective, scope={'include': ['Assigned objective in worker worktree'], 'exclude': ['Unrelated changes']},
                   constraints=list(project.constraints) + PROHIBITIONS, existing_changes='Inspect and preserve existing changes.',
                   task_retry=TaskRetryState(task_id=args.task_id, expected_write=bool(IMPLEMENTATION_PERMISSIONS & set(agent.permissions))).as_dict(),
                   required_validation=[json.dumps(list(argv), ensure_ascii=False) for argv in project.validation],
                   deliverables=['delegation-result JSON with validation and evidence'],
                   stop_conditions=['Stop on unclear authority, scope conflict, or private data exposure.'])
    validate(request, 'request')
    spec = '\n\n'.join(filter(None, ['Project: project:' + args.project + '\nAgent: @agent:' + agent.agent_id, 'Objective\n' + objective,
        'Scope\n' + json.dumps(request['scope']), 'Constraints\n' + '\n'.join(request['constraints']),
        'Prohibited effects\n' + '\n'.join(PROHIBITIONS), 'Validation commands (argv JSON)\n' + '\n'.join(request['required_validation']),
        'Deliverables\n' + '\n'.join(request['deliverables']), 'Stop conditions\n' + '\n'.join(request['stop_conditions']),
        'Manual gates (never clear these yourself)\n' + ('\n'.join(project.manual_gates) or 'None declared.'),
        presentation_context(agent, style), presentation_policy_context(layers.presentation_policy()),
        _report_section(request),
        'Placement: this machine (local), the coordinator selected this local project.']))
    # Reject private location metadata accidentally embedded in free text or style.
    private_values = [project.path, project.repository, *project.worktree_roots]
    if any(value and value in spec for value in private_values):
        raise ValueError('plan text contains private project location metadata')
    argv = ['orca', 'orchestration', 'worker-start', '--environment', 'local', '--spec-file', 'task-spec.txt', '--agent', project.runtime.agent]
    if project.runtime.workspace == 'existing':
        argv += ['--worktree', '<existing-worktree-selector>']
    else:
        argv += ['--worktree', 'new-top-level', '--repo', 'id:' + (args.orca_repo_id or '<repoId>'), '--name', args.task_id.lower()]
    output = {'request': request, 'spec': spec, 'argv': argv}
    if args.output_dir:
        directory = Path(args.output_dir).absolute()
        output['argv'][output['argv'].index('--spec-file') + 1] = str(directory / 'task-spec.txt')
        create_private_json(directory / 'dispatch-plan.json', output, repo_root=ROOT, label='dispatch plan', error=ValueError)
        create_private_json(directory / 'delegation-request.json', request, repo_root=ROOT, label='dispatch request', error=ValueError)
        # Same checked private writer, with text serialization supplied separately.
        from .private_files import create_private_text
        create_private_text(directory / 'task-spec.txt', spec, repo_root=ROOT, label='dispatch spec', error=ValueError)
    return output


def run(args):
    import sys
    try:
        return _run(args)
    except (UnicodeError, json.JSONDecodeError):
        print('Command refused: invalid text or JSON input.', file=sys.stderr)
    except ValueError as exc:
        print('Command refused: ' + str(exc), file=sys.stderr)
    except (OSError, RecursionError, TypeError):
        print('Command refused: invalid or unreadable input.', file=sys.stderr)
    return 1


def _run(args):
    from adapters.orca.result import read_json, map_result
    if args.command == 'dispatch-plan':
        output = plan(args)
    else:
        report = None
        if args.report:
            try:
                report = read_json(args.report)
            except ValueError:
                pass
        output = map_result(read_json(args.request), read_json(args.worker_done), load_registry(), report)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0
