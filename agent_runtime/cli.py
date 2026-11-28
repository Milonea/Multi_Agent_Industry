"""Local validation and setup commands for the framework configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .persona_layers import (
    PersonaSettingsError,
    initialize_personal_settings,
    load_persona_layers,
)
from .project_overlay import (
    ProjectOverlayError,
    apply_project_overlay,
    initialize_project_overlay,
    load_project_overlay,
)
from .registry import AgentRegistry, RegistryValidationError


def _registry(path: str) -> AgentRegistry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return AgentRegistry.from_dict(raw)


def _validate_registry(args: argparse.Namespace) -> int:
    registry = _registry(args.registry)
    print(
        json.dumps(
            {
                "status": "PASS",
                "registered_agents": len(registry.agents),
                "max_registered_agents": registry.max_registered_agents,
                "max_concurrent_agents": registry.max_concurrent_agents,
                "project_routes": len(registry.project_routes),
                "project_paths": len(registry.project_paths),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _validate_personas(args: argparse.Namespace) -> int:
    registry = _registry(args.registry)
    layers = load_persona_layers(
        registry,
        global_path=Path(args.global_settings),
        personal_path=Path(args.personal_settings) if args.personal_settings else None,
        repo_root=Path.cwd().resolve(),
        include_personal=not args.global_only,
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "global_profiles": len(layers.global_settings.profiles),
                "personal_profiles": len(layers.personal_settings.profiles),
                "global_agent_assignments": len(layers.global_settings.agent_assignments),
                "global_role_assignments": len(layers.global_settings.role_assignments),
                "personal_agent_assignments": len(layers.personal_settings.agent_assignments),
                "personal_role_assignments": len(layers.personal_settings.role_assignments),
                "global_policy_rules": len((layers.global_settings.presentation_policy or {}).get("rules", [])),
                "personal_policy_rules": len((layers.personal_settings.presentation_policy or {}).get("rules", [])),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _initialize_personas(args: argparse.Namespace) -> int:
    path = initialize_personal_settings(
        Path(args.personal_settings) if args.personal_settings else None,
        repo_root=Path.cwd().resolve(),
    )
    print(json.dumps({"status": "CREATED", "path": str(path)}, ensure_ascii=False))
    return 0


def _validate_project_overlay(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    registry = _registry(args.registry)
    overlay = load_project_overlay(
        registry,
        Path(args.project_overlay) if args.project_overlay else None,
        repo_root=repo_root,
    )
    effective = apply_project_overlay(registry, overlay, base_dir=repo_root)
    # Cross-file check: overlay persona assignments must reference loaded profiles.
    load_persona_layers(
        registry,
        global_path=Path(args.global_settings),
        personal_path=Path(args.personal_settings) if args.personal_settings else None,
        repo_root=repo_root,
        project_overlay=overlay,
    )
    counts = overlay.counts()
    # Counts only: project ids, paths, repositories and runtime ids stay private.
    print(
        json.dumps(
            {
                "status": "PASS",
                "overlay_projects": len(overlay.projects),
                "active_projects": counts["active"],
                "hold_projects": counts["hold"],
                "inventory_only_projects": counts["inventory_only"],
                "effective_project_routes": len(effective.project_routes),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _initialize_project_overlay(args: argparse.Namespace) -> int:
    path = initialize_project_overlay(
        Path(args.project_overlay) if args.project_overlay else None,
        repo_root=Path.cwd().resolve(),
    )
    print(json.dumps({"status": "CREATED", "path": str(path)}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    def run(args):
        from .orca_dispatch import run as run_orca
        return run_orca(args)
    dispatch = commands.add_parser('dispatch-plan', help='Plan an Orca dispatch without executing it')
    for name in ('project', 'role', 'objective-file', 'task-id'):
        dispatch.add_argument('--' + name, required=True)
    for name in ('agent', 'output-dir', 'orca-repo-id'):
        dispatch.add_argument('--' + name)
    dispatch.set_defaults(run=run)
    ingest = commands.add_parser('ingest-orca-result', help='Map a coordinator-supplied Orca receipt')
    ingest.add_argument('--request', required=True)
    ingest.add_argument('--worker-done', required=True)
    ingest.add_argument('--report')
    ingest.set_defaults(run=run)

    validate = commands.add_parser("validate", help="공통 Agent Registry 검사")
    validate.add_argument("--registry", default=".agents/registry.json", help="검사할 JSON 레지스트리 경로")
    validate.set_defaults(run=_validate_registry)

    validate_personas = commands.add_parser(
        "validate-personas", help="전역 및 개인 페르소나 설정 검사"
    )
    validate_personas.add_argument(
        "--registry", default=".agents/registry.json", help="Agent Registry 경로"
    )
    validate_personas.add_argument(
        "--global-settings",
        default=".agents/persona-settings.json",
        help="공유 페르소나 설정 경로",
    )
    persona_scope = validate_personas.add_mutually_exclusive_group()
    persona_scope.add_argument(
        "--personal-settings",
        help="검사할 개인 설정 파일의 절대 경로 (생략하면 사용자 기본 경로)",
    )
    persona_scope.add_argument(
        "--global-only",
        action="store_true",
        help="개인 설정 파일을 읽지 않고 공유 설정만 검사",
    )
    validate_personas.set_defaults(run=_validate_personas)

    initialize_personas = commands.add_parser(
        "init-personal-personas", help="빈 개인 페르소나 설정 파일 생성"
    )
    initialize_personas.add_argument(
        "--personal-settings",
        help="생성할 개인 설정 파일의 절대 경로 (생략하면 사용자 기본 경로)",
    )
    initialize_personas.set_defaults(run=_initialize_personas)

    validate_overlay = commands.add_parser(
        "validate-project-overlay", help="개인 프로젝트 오버레이 검사 (개인 페르소나와 교차 검증)"
    )
    validate_overlay.add_argument(
        "--registry", default=".agents/registry.json", help="Agent Registry 경로"
    )
    validate_overlay.add_argument(
        "--global-settings",
        default=".agents/persona-settings.json",
        help="공유 페르소나 설정 경로",
    )
    validate_overlay.add_argument(
        "--personal-settings",
        help="교차 검증할 개인 페르소나 설정의 절대 경로 (생략하면 사용자 기본 경로)",
    )
    validate_overlay.add_argument(
        "--project-overlay",
        help="검사할 개인 프로젝트 오버레이의 절대 경로 (생략하면 사용자 기본 경로)",
    )
    validate_overlay.set_defaults(run=_validate_project_overlay)

    initialize_overlay = commands.add_parser(
        "init-project-overlay", help="빈 개인 프로젝트 오버레이 파일 생성"
    )
    initialize_overlay.add_argument(
        "--project-overlay",
        help="생성할 개인 프로젝트 오버레이의 절대 경로 (생략하면 사용자 기본 경로)",
    )
    initialize_overlay.set_defaults(run=_initialize_project_overlay)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryValidationError) as exc:
        print(f"검증 실패: {exc}", file=sys.stderr)
        return 1
    except PersonaSettingsError as exc:
        print(f"페르소나 설정 실패: {exc}", file=sys.stderr)
        return 1
    except ProjectOverlayError as exc:
        print(f"프로젝트 오버레이 실패: {exc}", file=sys.stderr)
        return 1
