"""Repository documentation, schema and policy-integrity checks."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ImportError:
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.registry import AgentRegistry  # noqa: E402
from agent_runtime.persona_layers import PersonaSettings, PersonaSettingsError  # noqa: E402


REQUIRED_FILES = (
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
    ".codex/WORKFLOW.md",
    ".codex/PR_POLICY.md",
    ".codex/PROJECT_PROFILE.template.md",
    ".codex/PROJECT_PROFILE.md",
    ".github/CI_SETUP.template.md",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    "docs/architecture.md",
    "docs/getting-started.md",
    "docs/agent-configuration.md",
    "docs/runtime-adapters.md",
    "docs/security-model.md",
    "docs/migration-report.md",
    ".agents/persona-settings.json",
    ".agents/persona-settings.personal.example.json",
    ".agents/schemas/persona-settings.schema.json",
)
POLICY_MARKERS = {
    "AGENTS.md": (
        "기본 브랜치에 직접 푸시하지 않습니다",
        "강제 푸시",
        "실행하지 않은 검사를 통과로 보고하지 않습니다",
        "단일 통합 검증",
    ),
    ".codex/PR_POLICY.md": (
        "일상적인 코드, 테스트 및 문서 변경",
        "별도 사용자 승인을 요구하지 않습니다",
        "정책 무결성 검사",
        "누락된 검사 데이터를 성공으로 간주하지 않습니다",
    ),
    ".github/CI_SETUP.template.md": (
        "읽기 전용 권한",
        "전체 Commit SHA",
        "안정된 단일 통합 검사 상태",
        "누락된 검사 데이터는 통과가 아니라 차단",
    ),
    ".github/pull_request_template.md": (
        "## 요약",
        "## 범위",
        "## 검증",
        "## 위험과 수동 게이트",
        "## Rollback",
    ),
}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_files() -> None:
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).is_file()]
    if missing:
        fail("필수 파일 누락: " + ", ".join(missing))


def validate_policy_markers() -> None:
    for relative, markers in POLICY_MARKERS.items():
        content = (ROOT / relative).read_text(encoding="utf-8").casefold()
        absent = [marker for marker in markers if marker.casefold() not in content]
        if absent:
            fail(f"{relative} 정책 무결성 확인 실패: " + ", ".join(absent))


def validate_profile() -> None:
    content = (ROOT / ".codex/PROJECT_PROFILE.md").read_text(encoding="utf-8")
    if re.search(r"(?i)\b(?:TODO|TBD|REPLACE ME)\b", content):
        fail("프로젝트 프로파일에 미작성 안내가 남아 있습니다")
    for marker in ("Python 3.10", "python -m unittest discover -s tests -v", "python scripts/validate_repo.py", "Rollback"):
        if marker not in content:
            fail("프로젝트 프로파일에 확인된 운영 정보가 없습니다: " + marker)


def validate_persona_settings() -> None:
    try:
        global_settings = PersonaSettings.from_dict(
            json.loads((ROOT / ".agents/persona-settings.json").read_text(encoding="utf-8")),
            expected_scope="global",
        )
        personal_example = PersonaSettings.from_dict(
            json.loads(
                (ROOT / ".agents/persona-settings.personal.example.json").read_text(
                    encoding="utf-8"
                )
            ),
            expected_scope="personal",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, PersonaSettingsError) as exc:
        fail(f"공유 페르소나 설정 또는 빈 개인 예제 검증 실패: {exc}")
    if global_settings.scope != "global" or personal_example.scope != "personal":
        fail("페르소나 설정 범위가 올바르지 않습니다")
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    if ".agents/persona-settings.personal.json" not in ignore_rules:
        fail("저장소 로컬 개인 페르소나 파일이 gitignore에서 제외되지 않습니다")


def validate_runtime_permissions() -> int:
    registry = AgentRegistry.from_dict(
        json.loads((ROOT / ".agents/registry.json").read_text(encoding="utf-8"))
    )
    config_path = ROOT / ".codex/config.toml"
    config = config_path.read_text(encoding="utf-8")
    if tomllib is not None:
        config_data = tomllib.loads(config)
        agents_table = config_data.get("agents", {})
        configured_names = {
            name for name, value in agents_table.items() if isinstance(value, dict)
        }
    else:
        config_data = {}
        agents_table = {}
        configured_names = set(
            re.findall(r"(?m)^\[agents\.([a-z][a-z0-9_-]*)\]\s*$", config)
        )
    if configured_names != set(registry.agents):
        fail("Codex custom Agent 선언과 Agent Registry가 일치하지 않습니다")
    configured_concurrency = (
        agents_table.get("max_concurrent_threads_per_session")
        if tomllib is not None
        else None
    )
    if configured_concurrency is None:
        match = re.search(
            r"(?m)^max_concurrent_threads_per_session\s*=\s*(\d+)\s*$", config
        )
        configured_concurrency = int(match.group(1)) if match is not None else None
    if configured_concurrency != registry.max_concurrent_agents:
        fail("Codex 동시 실행 설정과 Registry 제한이 일치하지 않습니다")

    for agent in registry.agents.values():
        if tomllib is not None:
            section = agents_table.get(agent.agent_id)
            file_name = section.get("config_file") if isinstance(section, dict) else None
        else:
            config_match = re.search(
                rf"(?ms)^\[agents\.{re.escape(agent.agent_id)}\]\s*$"
                rf"(.*?)(?=^\[|\Z)",
                config,
            )
            file_match = (
                re.search(r'(?m)^config_file\s*=\s*"([^"]+)"\s*$', config_match.group(1))
                if config_match is not None
                else None
            )
            file_name = file_match.group(1) if file_match is not None else None
        if not isinstance(file_name, str):
            fail(f"Codex Agent 섹션이 없습니다: {agent.agent_id}")
        agent_path = (ROOT / ".codex" / file_name).resolve()
        try:
            agent_path.relative_to(ROOT / ".codex")
        except ValueError:
            fail(f"Codex Agent 설정 파일이 .codex 경계를 벗어납니다: {agent.agent_id}")
        if not agent_path.is_file():
            fail(f"Codex Agent 설정 파일이 없습니다: {agent.agent_id}")
        text = agent_path.read_text(encoding="utf-8")
        if tomllib is not None:
            agent_data = tomllib.loads(text)
            configured_name = agent_data.get("name")
            configured_sandbox = agent_data.get("sandbox_mode")
        else:
            name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"\s*$', text)
            sandbox_match = re.search(
                r'(?m)^sandbox_mode\s*=\s*"(read-only|workspace-write)"\s*$', text
            )
            configured_name = name_match.group(1) if name_match is not None else None
            configured_sandbox = sandbox_match.group(1) if sandbox_match is not None else None
        expected_sandbox = "workspace-write" if "write_workspace" in agent.permissions else "read-only"
        if configured_name != agent.agent_id:
            fail(f"Codex Agent 설정 이름이 Registry와 다릅니다: {agent.agent_id}")
        if configured_sandbox != expected_sandbox:
            fail(f"Codex sandbox_mode가 선언된 권한과 다릅니다: {agent.agent_id}")
        if "write_external" in agent.permissions or "deploy" in agent.permissions:
            fail(f"기본 공개 Registry에 외부 쓰기 또는 배포 권한이 있습니다: {agent.agent_id}")
    return len(registry.agents)


def validate_json_files() -> int:
    count = 0
    for path in ROOT.rglob("*.json"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path == ROOT / ".agents/persona-settings.personal.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"JSON 파싱 실패: {path.relative_to(ROOT)} ({exc})")
        if path.name.endswith(".schema.json"):
            if not isinstance(data, dict) or "$schema" not in data or "$id" not in data:
                fail(f"Schema 식별 정보가 없습니다: {path.relative_to(ROOT)}")
        count += 1
    return count


def validate_markdown_links() -> int:
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    count = 0
    for path in ROOT.rglob("*.md"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(content):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            relative = unquote(parsed.path)
            if not relative:
                continue
            destination = (path.parent / relative).resolve()
            try:
                destination.relative_to(ROOT)
            except ValueError:
                fail(f"저장소 밖을 가리키는 Markdown 링크: {path.relative_to(ROOT)} -> {target}")
            if not destination.exists():
                fail(f"대상 파일이 없는 Markdown 링크: {path.relative_to(ROOT)} -> {target}")
            count += 1
    return count


def main() -> int:
    validate_files()
    validate_policy_markers()
    validate_profile()
    validate_persona_settings()
    agent_count = validate_runtime_permissions()
    json_count = validate_json_files()
    link_count = validate_markdown_links()
    toml_count = 0
    toml_status = "NOT_CHECKED"
    if tomllib is not None:
        for path in ROOT.rglob("*.toml"):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                fail(f"TOML 파싱 실패: {path.relative_to(ROOT)} ({exc})")
            toml_count += 1
        toml_status = "PASS"
    print(json.dumps({"status": "PASS", "agents": agent_count, "json_files": json_count, "markdown_links": link_count, "persona_settings": "PASS", "policy_integrity": "PASS", "runtime_permissions": "PASS", "toml_files": toml_count, "toml_syntax": toml_status}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
