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

선택적 persona 객체에는 profile_id, locale, style 및 tone_tags만 둘 수 있습니다. Agent Runtime은 이를 짧은 표현 Context로 변환할 수 있습니다. 사용자가 원하는 표현을 바꿔도 agent_id, role, permissions, Runtime Binding 및 project_routes는 바뀌지 않습니다.

완성된 개인 프로파일 저장소, 프로파일 선택기, 개인 대화 이력 및 UI는 포함하지 않습니다. 예시는 합성 값으로만 작성합니다.

## 변경과 검증

Agent를 추가하거나 권한을 바꾸면 Registry Schema, .codex/agents 설정, 관련 계약 테스트 및 정책 무결성 검사를 함께 갱신합니다. 권한 목록은 필요한 최소 권한으로 유지하고 외부 쓰기 권한은 별도 승인을 우회할 수 없도록 해야 합니다.
