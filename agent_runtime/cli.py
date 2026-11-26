"""Small local CLI for validating the framework-only configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .registry import AgentRegistry, RegistryValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_runtime")
    parser.add_argument("command", choices=("validate",), help="등록 설정의 로컬 유효성 검사")
    parser.add_argument("--registry", default=".agents/registry.json", help="검사할 JSON 레지스트리 경로")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        registry = AgentRegistry.from_dict(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryValidationError) as exc:
        print(f"검증 실패: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "registered_agents": len(registry.agents),
                "max_registered_agents": registry.max_registered_agents,
                "max_concurrent_agents": registry.max_concurrent_agents,
                "project_routes": len(registry.project_routes),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
