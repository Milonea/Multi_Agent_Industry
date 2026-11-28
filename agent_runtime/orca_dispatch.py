"""Produce reviewable local Orca plans; never invoke Orca."""
import json
import os
from pathlib import Path

from .delegation_contract import ROOT, validate
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


def plan(args):
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
                   stop_conditions=['Stop on unclear authority, scope conflict, or private data exposure.'] + list(project.manual_gates))
    validate(request, 'request')
    spec = '\n\n'.join(['Project: project:' + args.project, 'Objective\n' + objective,
        'Scope\n' + json.dumps(request['scope']), 'Constraints\n' + '\n'.join(request['constraints']),
        'Prohibited effects\n' + '\n'.join(PROHIBITIONS), 'Validation commands (argv JSON)\n' + '\n'.join(request['required_validation']),
        'Deliverables\n' + '\n'.join(request['deliverables']), 'Stop conditions\n' + '\n'.join(request['stop_conditions']),
        presentation_context(agent, style), presentation_policy_context(layers.presentation_policy()),
        'Report\nWrite a delegation-result JSON matching contract 2.0 and the request identity below. Include commands, validation and evidence. Pass its path with Orca worker_done --report-path.\n' + json.dumps(request, ensure_ascii=False),
        'Placement: this machine (local), the coordinator selected this local project.'])
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
        create_private_json(directory / 'dispatch-plan.json', output, repo_root=ROOT, label='dispatch plan', error=ValueError)
        create_private_json(directory / 'delegation-request.json', request, repo_root=ROOT, label='dispatch request', error=ValueError)
        # Same checked private writer, with text serialization supplied separately.
        from .private_files import create_private_text
        create_private_text(directory / 'task-spec.txt', spec, repo_root=ROOT, label='dispatch spec', error=ValueError)
        output['argv'][output['argv'].index('--spec-file') + 1] = str(directory / 'task-spec.txt')
    return output


def run(args):
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
