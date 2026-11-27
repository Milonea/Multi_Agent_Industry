# Agent 설정

## Agent Registry

.agents/registry.json은 저장소에서 공유할 수 있는 범용 Agent 정의를 관리합니다. .agents/schemas/agent-registry.schema.json과 agent_runtime.registry.AgentRegistry가 구조를 검증합니다.

각 Agent 정의의 핵심 필드는 다음과 같습니다.

| 필드 | 목적 |
| --- | --- |
| agent_id | Agent Instance 식별자 |
| role | 업무 책임 분류. 여러 Agent가 같은 Role을 가질 수 있음 |
| capabilities | 할 수 있는 업무 범위 |
| responsibilities | 해당 Instance가 담당하는 업무 |
| permissions | 실행 권한. 역할이나 표현 설정과 독립적으로 검사 |
| validation_requirements | 결과를 수락하기 위해 필요한 확인 |
| runtime_binding | 실제 Runtime 공급자, Agent 이름 및 활성 상태 |
| escalation_policy | 한계를 만나면 중단·인계할 조건 |
| persona | 선택 가능한 표현 정보. 권한이나 라우트를 포함할 수 없음 |

Registry의 permissions는 업무 계약에 선언된 권한 경계이며, 그 자체로 Codex의 도구 권한을 강제하지 않습니다. Codex Adapter에서는 Agent별 sandbox_mode가 실제 파일 접근 경계를 제한합니다. 현재 Implementer와 Documentation Specialist만 workspace-write를 사용하며 나머지 역할은 read-only입니다. 다른 Runtime Adapter는 해당 플랫폼에서 동등한 실행 권한 검사를 별도로 구현해야 합니다.

## 프로젝트 라우트 (project_routes)

`project_routes`는 Project ID를 담당 Agent에 연결합니다. 기본 Registry에서는 비어 있습니다. 값은 기존처럼 Agent ID 문자열이거나, `agent_id`와 선택 필드 `path`를 가진 객체입니다.

```json
"project_routes": {
  "sample-project": "implementer",
  "sample-docs": { "agent_id": "documentation_specialist", "path": "examples/sample-docs" }
}
```

`path`가 있으면 Codex 이벤트의 작업 디렉터리가 그 경로이거나 하위 경로일 때 해당 Project가 선택됩니다. 상대 경로는 이 저장소 루트를 기준으로 해석합니다. 절대 경로도 허용하지만 공개 저장소에는 실제 사용자 경로를 커밋하지 않고 로컬·비공개 Registry에서만 사용합니다. `~` 확장, `..` 구간, 앞뒤 공백, 같은 경로의 중복 선언은 검증 단계에서 거부합니다. 중첩된 경로는 더 깊은 경로가 우선하며, 해석 결과가 같은 깊이에서 둘 이상 일치하면 Hook이 차단합니다. 명시 `project:<id>` 토큰과 같은 범위의 기존 상태가 작업 디렉터리보다 우선합니다. `path`는 라우팅 선택에만 쓰이며 파일 접근이나 다른 권한을 부여하지 않습니다. 실제 사용자 프로젝트는 아래 개인 프로젝트 오버레이에 둡니다.

## 개인 프로젝트 오버레이

사용자의 실제 프로젝트는 공개 Registry가 아니라 저장소 밖의 개인 파일 `project-overlay.json`에 둡니다. 오버레이는 10개 범용 Role에 프로젝트를 연결할 뿐이며 프로젝트별 Agent Instance를 만들지 않습니다. 공개 저장소에는 Schema(`.agents/schemas/project-overlay.schema.json`), 로더(`agent_runtime/project_overlay.py`), 합성 예제(`.agents/project-overlay.personal.example.json`)와 테스트만 있습니다.

| 항목 | 위치 |
| --- | --- |
| 기본 경로 | 개인 페르소나 설정과 같은 디렉터리의 `project-overlay.json` (macOS `~/Library/Application Support/MultiAgentIndustry/`, Windows `%APPDATA%/MultiAgentIndustry/`, Linux `$XDG_CONFIG_HOME/multi-agent-industry/` 또는 `~/.config/multi-agent-industry/`) |
| 경로 재지정 | `MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY` (절대 경로만) |
| 생성·검사 | `python -m agent_runtime init-project-overlay`, `python -m agent_runtime validate-project-overlay` |

파일 안전 규칙은 개인 페르소나 설정과 같습니다. 저장소 밖에 있어야 하고 심볼릭 링크가 아니어야 하며, POSIX에서는 파일 0600·디렉터리 0700이고 현재 사용자 소유여야 합니다. 파일이 없으면 빈 오버레이로 동작합니다. 파일이 있으나 형식·권한·교차 검증이 틀리면 Hook과 CLI는 내용을 출력하지 않고 차단합니다. `init-project-overlay`는 빈 파일만 만들고 기존 파일을 덮어쓰지 않습니다. `validate-project-overlay`는 개인 페르소나 설정도 함께 읽어 프로필 참조를 교차 검증하고, 프로젝트 이름이나 경로 없이 개수만 출력합니다.

Schema v1.0의 최상위 필드는 `schema_version`("1.0"), `scope`("personal"), `projects`입니다. 프로젝트별 필드는 다음과 같으며 그 밖의 키는 거부합니다.

| 필드 | 필수 | 의미 |
| --- | --- | --- |
| `owner_agent_id` | 예 | 프로젝트 담당 Agent. Registry에 있어야 함 |
| `status` | 예 | `active`, `hold`, `inventory_only` |
| `hold_agent_id` | `hold`일 때만 | 보류 중 대신 라우팅할 Agent. 구현 가능 권한(`write_workspace`·`write_external`·`deploy`)이 없어야 함 |
| `path` | 아니요 | 프로젝트 절대 경로. 작업 디렉터리 매칭에 사용 |
| `worktree_roots` | 아니요 | 추가 절대 경로 목록(예: 별도 Worktree 위치). 최대 16개 |
| `repository` | 아니요 | `owner/name`. 표시·메타데이터 전용이며 라우팅이나 Hook Context에 쓰지 않음 |
| `persona_assignments` | 아니요 | `{"roles": {role: profile_id}, "agents": {agent_id: profile_id}}`. 프로필은 개인 또는 공유 페르소나 설정에 있어야 함 |
| `constraints` | 아니요 | 작업을 좁히는 짧은 문장 목록(최대 32개, 각 300자). Hook Context에 전달되며 권한을 부여하지 않음 |
| `validation` | 아니요 | 검증 명령 argv 목록(예: `["python", "-m", "unittest"]`). 저장·검증만 하며 실행하지 않음 |
| `manual_gates` | 아니요 | 수동 게이트 이름 목록 |
| `runtime` | 아니요 | `provider`("orca"), `agent`(`codex`·`claude`), 선택 `orca_project_id`, 선택 `workspace`(`new-top-level`·`existing`). 현재는 저장·검증만 함 |

`permissions`, `capabilities`, `runtime_binding`, `tools`, `commands`, `routes` 같은 권한·실행 키는 위치와 관계없이 거부합니다. 예외는 Schema 필드인 `persona_assignments.roles`뿐입니다. 자유 텍스트 별칭은 지원하지 않으므로 프롬프트 속 이름으로 프로젝트를 추정하지 않습니다.

### 결합과 우선순위

유효 프로젝트 라우트는 Registry `project_routes`와 오버레이 `projects`의 합집합입니다. 두 계층에서 같은 Project ID를 쓰거나, 해석한 경로(`path`·`worktree_roots`)가 다른 항목과 겹치면 차단합니다. 오버레이가 공유 라우트를 조용히 덮어쓰지 않습니다.

| 순서 | Agent 선택 | Project 선택 |
| --- | --- | --- |
| 1 | `@agent:<id>` | `project:<id>` |
| 2 | `role:<role>` | 같은 범위의 기존 상태(continuity) |
| 3 | `project:<id>` 라우트 | 작업 디렉터리(가장 깊은 `path`·`worktree_roots`, 같은 깊이면 차단) |
| 4 | continuity | 없음 |
| 5 | 작업 디렉터리 Project 라우트 | |
| 6 | Registry 기본 Agent | |

상태별 동작은 다음과 같습니다.

| status | 라우트 Agent | 제한 |
| --- | --- | --- |
| `active` | `owner_agent_id` | 없음 |
| `hold` | `hold_agent_id` | 대상 Project가 hold이면 구현 가능 Agent를 `@agent:`, `role:`, continuity, `support:agent:` 어느 경로로도 거부 |
| `inventory_only` | 없음 | `project:<id>`는 차단. 작업 디렉터리의 가장 깊은 일치가 inventory 항목이면 Project 없이 기본 Agent로 Fallback(상위 Project로 넘어가지 않음) |

## 역할과 Agent Instance

Role은 구현, 검토, 조사 같은 업무를 설명합니다. Agent ID는 특정 실행 인스턴스를 지정합니다. 동일 Role에 여러 Instance가 있으면 role:<role> 라우트는 모호한 요청으로 처리됩니다. 해당 경우 @agent:<id>를 사용하거나 Coordinator가 선택 근거를 정리해야 합니다.

등록 가능한 수는 Registry의 max_registered_agents가 정합니다. 동시에 실행할 수 있는 수는 max_concurrent_agents와 Runtime의 더 낮은 한도 중 작은 값으로 제한합니다. 등록 용량이 곧 병렬 실행 보장을 의미하지 않습니다.

병렬 실행은 다음 조건을 모두 충족할 때만 사용합니다.

- 각 Worker의 파일, 데이터와 외부 상태 소유권이 서로 겹치지 않습니다.
- Coordinator가 Worker마다 독립된 업무 계약, 입력·산출물, 검증 순서와 중단 조건을 지정했습니다.
- 한 작업의 결과가 다른 작업의 입력이 아닙니다. 의존 관계나 소유권 충돌이 있으면 앞선 결과를 검증한 뒤 순차적으로 인계합니다.

Worker는 다른 Worker를 생성하지 않으며, 사용자 대화를 이끄는 활성 담당자는 한 명으로 유지합니다. 동시 실행 상한을 바꿀 때는 Registry의 max_concurrent_agents와 .codex/config.toml의 max_concurrent_threads_per_session을 함께 변경해야 하며 scripts/validate_repo.py가 둘의 일치를 검사합니다.

현재 기본 역할은 Coordinator, Architect, Implementer, Validator, Reviewer, Researcher, Documentation Specialist, Release Manager, Incident Commander 및 Fallback Investigator입니다. Fallback Investigator는 읽기 전용 저위험 조사만 수행하며 수정·배포 권한이 없습니다.

## 페르소나와 표현 정보

페르소나 설정은 저장소에서 공유하는 전역 기본값과 사용자별 개인 설정으로 나뉩니다.

| 범위 | 파일 | 용도 |
| --- | --- | --- |
| 공유 전역 | `.agents/persona-settings.json` | 프로젝트에서 함께 쓰는 프로파일과 기본 배정. Git으로 공유합니다. |
| 개인 | 사용자 설정 디렉터리의 `persona-settings.json` | 개인 프로파일, Agent·Role 배정 및 개인 기본값. 저장소 밖에 두며 Git에 포함하지 않습니다. |
| 개인 예제 | `.agents/persona-settings.personal.example.json` | 빈 설정 파일 구조 안내. 실제 사용자 값을 넣지 않습니다. |

공유 전역은 운영체제 전체 설정이 아니라 이 저장소의 팀 공통 설정입니다. 개인 설정은 여러 저장소에서 재사용할 수 있는 사용자별 설정입니다. 기본 개인 경로는 macOS의 `~/Library/Application Support/MultiAgentIndustry/persona-settings.json`, Windows의 `%APPDATA%/MultiAgentIndustry/persona-settings.json`, Linux의 `$XDG_CONFIG_HOME/multi-agent-industry/persona-settings.json`입니다. Linux에서 `XDG_CONFIG_HOME`이 설정되지 않으면 `~/.config/multi-agent-industry/persona-settings.json`을 사용합니다. `MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS` 환경 변수로 절대 경로를 지정할 수 있습니다.

현재 적용 우선순위는 다음과 같습니다. 1~2단계는 라우트의 활성 Project가 개인 프로젝트 오버레이에 있을 때만 적용합니다.

1. 프로젝트 Agent 배정 (`persona_assignments.agents`)
2. 프로젝트 Role 배정 (`persona_assignments.roles`)
3. 개인 Agent 배정
4. 개인 Role 배정
5. 개인 기본 프로파일
6. 공유 전역 Agent 배정
7. Registry에 직접 적은 Agent persona
8. 공유 전역 Role 배정
9. 공유 전역 기본 프로파일

개인 배정은 개인 프로파일이나 공유 프로파일을 참조할 수 있습니다. 공유 전역 설정은 공유 프로파일만 참조합니다. 같은 프로파일 ID를 두 범위에서 재사용하지 않습니다. 공유 전역 프로파일은 `profile_id`, `locale`, `style`, `tone_tags`만 허용합니다. 개인 프로파일에는 여기에 `identity_data`와 `source`를 더해 상세 인물 정보, 말투, 반응 특성, 관계와 가져온 원본의 해시를 저장할 수 있습니다. 이 상세 정보와 출처는 개인 설정 파일에만 두며 Hook은 출처 메타데이터를 Context로 전달하지 않습니다.

개인 상세 프로파일은 표현 참고 정보입니다. 업무 역할, 권한, 라우팅, Runtime Binding, Capabilities, 명령 또는 사실성 기준을 바꿀 수 없습니다. 대상 Agent와 실제 권한은 계속 Registry와 Runtime 정책이 결정합니다. 개인 설정은 사용자별 파일에서만 관리하고, 공개 저장소에는 빈 예제 구조만 둡니다.

Codex Hook은 선택된 표현 정보만 Context로 전달합니다. 개인 설정 파일은 POSIX에서 소유자 전용 디렉터리(0700)와 파일(0600)을 요구합니다. 개인 설정은 라우팅 상태나 대화 이력에 복사하지 않습니다. Windows ACL은 런타임에서 직접 검사하지 않습니다.

## 표현 규칙 (presentation_policy)

공유·개인 설정 파일은 선택 필드 `presentation_policy`로 페르소나 표현 규칙을 둘 수 있습니다. 규칙은 말투와 노출 방식만 조정하며 근거, 검증 상태, 위험도, 권한, 라우팅 또는 상위 지침을 바꾸지 않습니다.

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `rules` | 최대 32개, 각 500자 이하의 한 줄 문자열 | 호칭·이름 사용·보고 방식 같은 표현 규칙 |
| `presence` | `casual`, `coding`, `validation`, `security`, `destructive_operation` → `off`·`low`·`medium_low`·`medium`·`high` | 작업 맥락별 페르소나 존재감. `security`와 `destructive_operation`은 `medium_low`를 넘을 수 없음 |
| `dialogue_budget` | `trivial_task`, `normal_task`, `complex_task`, `critical_validation` → 0~20 정수 | 맥락별 최대 페르소나 발화 수 |

병합 시 `rules`는 공유 규칙 뒤에 개인 규칙을 중복 없이 덧붙이고, `presence`와 `dialogue_budget`은 키별로 개인 값이 공유 값보다 우선합니다. 공유 기본값에는 특정 인물이나 작품에 묶이지 않은 일반 규칙만 둡니다. 특정 페르소나의 호칭, 관계 우선순위나 세계관 규칙은 개인 설정 파일에만 저장합니다. Codex Hook은 해석된 페르소나가 있을 때에만 병합된 규칙을 Context에 덧붙이며, 규칙은 라우팅 상태에 저장하지 않습니다.

설정을 시작하려면 다음 명령으로 사용자 설정 파일을 만들 수 있습니다. 기존 파일을 덮어쓰지 않으며 빈 설정만 생성합니다.

    python -m agent_runtime init-personal-personas
    python -m agent_runtime validate-personas

`init-personal-personas`는 파일이 없을 때에만 빈 개인 설정을 만듭니다. 개인 파일을 읽지 않고 공유 파일만 검사하려면 `python -m agent_runtime validate-personas --global-only`를 사용합니다. 기본 `validate-personas`는 개인 파일이 존재하면 이를 읽어 함께 검사합니다.

## 변경과 검증

Agent를 추가하거나 권한을 바꾸면 Registry Schema, .codex/agents 설정, 관련 계약 테스트 및 정책 무결성 검사를 함께 갱신합니다. 권한 목록은 필요한 최소 권한으로 유지하고 외부 쓰기 권한은 별도 승인을 우회할 수 없도록 해야 합니다.
