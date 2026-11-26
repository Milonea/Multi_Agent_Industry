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

## 역할과 Agent Instance

Role은 구현, 검토, 조사 같은 업무를 설명합니다. Agent ID는 특정 실행 인스턴스를 지정합니다. 동일 Role에 여러 Instance가 있으면 role:<role> 라우트는 모호한 요청으로 처리됩니다. 해당 경우 @agent:<id>를 사용하거나 Coordinator가 선택 근거를 정리해야 합니다.

등록 가능한 수는 Registry의 max_registered_agents가 정합니다. 동시에 실행할 수 있는 수는 max_concurrent_agents와 Runtime의 더 낮은 한도 중 작은 값으로 제한합니다. 등록 용량이 곧 병렬 실행 보장을 의미하지 않습니다.

현재 기본 역할은 Coordinator, Architect, Implementer, Validator, Reviewer, Researcher, Documentation Specialist, Release Manager, Incident Commander 및 Fallback Investigator입니다. Fallback Investigator는 읽기 전용 저위험 조사만 수행하며 수정·배포 권한이 없습니다.

## 페르소나와 표현 정보

페르소나 설정은 저장소에서 공유하는 전역 기본값과 사용자별 개인 설정으로 나뉩니다.

| 범위 | 파일 | 용도 |
| --- | --- | --- |
| 공유 전역 | `.agents/persona-settings.json` | 프로젝트에서 함께 쓰는 프로파일과 기본 배정. Git으로 공유합니다. |
| 개인 | 사용자 설정 디렉터리의 `persona-settings.json` | 개인 프로파일, Agent·Role 배정 및 개인 기본값. 저장소 밖에 두며 Git에 포함하지 않습니다. |
| 개인 예제 | `.agents/persona-settings.personal.example.json` | 빈 설정 파일 구조 안내. 실제 사용자 값을 넣지 않습니다. |

공유 전역은 운영체제 전체 설정이 아니라 이 저장소의 팀 공통 설정입니다. 개인 설정은 여러 저장소에서 재사용할 수 있는 사용자별 설정입니다. 기본 개인 경로는 macOS의 `~/Library/Application Support/MultiAgentIndustry/persona-settings.json`, Windows의 `%APPDATA%/MultiAgentIndustry/persona-settings.json`, Linux의 `$XDG_CONFIG_HOME/multi-agent-industry/persona-settings.json`입니다. Linux에서 `XDG_CONFIG_HOME`이 설정되지 않으면 `~/.config/multi-agent-industry/persona-settings.json`을 사용합니다. `MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS` 환경 변수로 절대 경로를 지정할 수 있습니다.

현재 적용 우선순위는 다음과 같습니다.

1. 개인 Agent 배정
2. 개인 Role 배정
3. 개인 기본 프로파일
4. 공유 전역 Agent 배정
5. Registry에 직접 적은 Agent persona
6. 공유 전역 Role 배정
7. 공유 전역 기본 프로파일

개인 배정은 개인 프로파일이나 공유 프로파일을 참조할 수 있습니다. 공유 전역 설정은 공유 프로파일만 참조합니다. 같은 프로파일 ID를 두 범위에서 재사용하지 않습니다. 프로파일은 `profile_id`, `locale`, `style`, `tone_tags`만 허용하며 권한, 업무 역할, 라우팅, Runtime Binding, Capabilities 또는 실행 명령을 정의할 수 없습니다.

Codex Hook은 선택된 표현 정보만 Context로 전달합니다. 개인 설정 파일은 POSIX에서 소유자 전용 디렉터리(0700)와 파일(0600)을 요구합니다. 개인 설정은 라우팅 상태나 대화 이력에 복사하지 않습니다. Windows ACL은 런타임에서 직접 검사하지 않습니다.

설정을 시작하려면 다음 명령으로 사용자 설정 파일을 만들 수 있습니다. 기존 파일을 덮어쓰지 않으며 빈 설정만 생성합니다.

    python -m agent_runtime init-personal-personas
    python -m agent_runtime validate-personas

`init-personal-personas`는 파일이 없을 때에만 빈 개인 설정을 만듭니다. 개인 파일을 읽지 않고 공유 파일만 검사하려면 `python -m agent_runtime validate-personas --global-only`를 사용합니다. 기본 `validate-personas`는 개인 파일이 존재하면 이를 읽어 함께 검사합니다.

## 변경과 검증

Agent를 추가하거나 권한을 바꾸면 Registry Schema, .codex/agents 설정, 관련 계약 테스트 및 정책 무결성 검사를 함께 갱신합니다. 권한 목록은 필요한 최소 권한으로 유지하고 외부 쓰기 권한은 별도 승인을 우회할 수 없도록 해야 합니다.
