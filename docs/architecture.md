# 아키텍처

Multi Agent Industry는 역할 정의, 업무 계약, 실행 Runtime 및 외부 서비스를 분리합니다. Core는 특정 사용자 페르소나, 등록된 프로젝트 또는 외부 계정 없이 초기화할 수 있습니다.

## 구성 요소

| 구성 요소 | 현재 구현 | 책임 |
| --- | --- | --- |
| Agent Runtime | agent_runtime | Agent Registry, Role, 권한, Runtime Binding, 라우팅, 세션 상태, Handoff 수락 및 공유·개인 표현 설정 병합 |
| Codex Adapter | adapters/codex, .codex | Codex UserPromptSubmit Hook, 사용자 선택 토큰 해석, 페르소나 Context 전달, Codex 사용자 정의 Agent 설정 |
| Portfolio Control Plane | portfolio_control_plane | 목표와 결과 계약, Capability Registry, 정책, 상태·현실 점검, 의존성, 선택, 재시도, 근거, 관찰성 및 복구 |
| Contracts | .agents/contracts, agents/shared/schemas, portfolio_control_plane/contracts/schemas | Agent 간 요청·결과·이벤트와 Core 데이터 구조의 버전 계약 |
| Optional Integration | 현재 외부 서비스 Adapter 없음 | 향후 외부 시스템 연결을 Core와 별도 모듈로 격리할 확장 지점 |

## Agent 정의와 실행 인스턴스

.agents/registry.json은 공개된 공통 Agent 정의를 보관합니다. Agent ID는 인스턴스를 구분하고 Role은 업무 책임을 나타냅니다. Capabilities, Responsibilities, Permissions, Validation Requirements, Runtime Binding 및 Escalation Policy는 별도 필드입니다.

같은 Role에 복수 Agent를 등록할 수 있습니다. Role만 지정했는데 활성 Agent가 둘 이상이면 임의 선택하지 않고 명시적 Agent ID를 요구합니다. 최대 등록 수와 동시 실행 수는 별도 제한이며, 실제 동시 실행은 Runtime이 제공하는 기능과 한도를 따라야 합니다.

초기 역할은 Coordinator, Architect, Implementer, Validator, Reviewer, Researcher, Documentation Specialist, Release Manager, Incident Commander 및 제한된 Fallback Investigator입니다. Registry와 .codex/agents의 구체 정의를 확인한 뒤 필요한 역할만 지연 활성화합니다.

## 요청 라우팅과 대화 상태

Codex Hook은 @agent:<id>, role:<role>, project:<id>, support:agent:<id> 토큰을 해석합니다. 유효 Project 라우트는 공개 Registry의 `project_routes`와 저장소 밖 개인 프로젝트 오버레이의 합집합이며, ID나 경로가 겹치면 차단합니다([Agent 설정](agent-configuration.md#개인-프로젝트-오버레이)). 우선순위는 다음과 같으며 먼저 일치한 단계가 적용됩니다.

1. 명시 Agent (`@agent:<id>`)
2. 명시 Role (`role:<role>`, 활성 Agent가 정확히 하나일 때만)
3. 명시 Project 라우트 (`project:<id>`)
4. 같은 세션·작업 디렉터리 범위의 기존 담당자 (`route_source=continuity`)
5. 작업 디렉터리 Project: Codex 이벤트의 `cwd`가 등록된 Project 라우트의 `path` 안에 있으면 해당 Project의 담당 Agent (`route_source=working_directory`)
6. Registry 기본 Agent로의 마지막 Fallback (`route_source=coordinator`)

작업 디렉터리 매칭은 Registry 라우트의 `path`와 개인 프로젝트 오버레이의 `path`·`worktree_roots`를 함께 보고 가장 깊은 경로를 고릅니다. 같은 깊이에서 둘 이상 일치하면 차단합니다.

활성 Project도 명시 Project, 기존 상태, 작업 디렉터리 Project 순서로 정합니다. 활성 Project의 상태가 `hold`이면 Project 라우트는 `hold_agent_id`를 선택하고, 구현 가능 권한을 가진 Agent는 명시 Agent·Role·기존 상태·지원 Agent 어느 경로로도 거부합니다. `inventory_only` Project는 라우팅하지 않습니다. 프롬프트 본문의 자유 텍스트 이름이나 별칭으로 Agent·Project를 추정하지 않습니다. 프로젝트 ID와 Agent ID가 미등록되었거나 Runtime Binding이 비활성화된 경우, 또는 작업 디렉터리가 같은 깊이의 Project 경로 둘 이상과 일치하는 경우 라우팅을 차단합니다.

Hook은 라우트 결과와 등록된 Runtime Binding을 짧은 추가 Context로 Codex에 전달합니다. Hook 자체는 하위 Agent를 실행하거나 권한을 부여하지 않습니다. 가능한 경우 Codex Runtime이 등록 Agent를 실제로 시작해야 합니다. 실행을 확인할 수 없으면 계획 또는 차단으로 보고합니다. 근거가 있을 때만 결과를 실행된 Agent의 작업으로 인정하며, 최종 응답 소유자는 Coordinator로 돌아갑니다.

대화 상태는 버전, 활성 Agent ID, 활성 Project ID 및 지원 Agent ID만 포함합니다. 상태 범위는 세션 ID와 작업 디렉터리를 함께 Hash한 값이므로 같은 세션에서도 다른 디렉터리로 이동하면 이전 담당자를 이어받지 않습니다. 프롬프트, Transcript, 도구 출력, 사용자 프로파일 및 인증정보는 상태로 저장하지 않습니다.

페르소나 계층은 저장소의 `.agents/persona-settings.json`에 있는 간단한 공유 표현 설정과 저장소 밖 사용자 설정 파일을 읽습니다. 개인 파일만 상세 인물 정보, 말투, 관계와 원본 해시를 포함할 수 있습니다. 활성 Project의 오버레이 Agent·Role 배정, 개인 Agent·Role 배정과 개인 기본값을 공유 설정 위에 차례로 적용한 뒤 선택된 인물 표현과 공유·개인 `presentation_policy`를 병합한 표현 규칙만 Hook Context에 덧붙이고 출처 메타데이터는 전달하지 않습니다. 페르소나는 표현 전용이며 라우팅, 권한, Capabilities 또는 Runtime Binding을 바꾸지 않습니다. 파일 경로와 적용 순서는 [Agent 설정](agent-configuration.md)에 정의되어 있습니다.

## Task, Result 및 Handoff 계약

Task/Result 계약은 요청 목표, 작업 범위와 제외 사항, 입력, 산출물, 실행 권한, 검증 조건, 중단 조건, 실제 실행 상태, 근거, 위험 및 후속 작업을 기록합니다. JSON Schema는 .agents/contracts와 portfolio_control_plane/contracts/schemas에 두며, 버전 변경은 이전 데이터와 호출자 영향을 함께 검토해야 합니다.

외부 부수 효과가 승인되어도 설정된 Adapter가 없으면 실행하지 않고 NEEDS_MANUAL 상태를 반환합니다. 필수 검증 누락, 권한 초과, 미등록 Capability 및 실제 실행을 입증할 수 없는 Handoff는 PASS로 승격하지 않습니다.

## Portfolio Control Plane

- Reality Scanner는 명시적으로 제공한 저장소만 점검합니다. 기본 저장소 목록은 비어 있습니다.
- State Ledger와 Observability 계약은 업무 상태와 최소한의 추적 근거를 표현합니다. 이 저장소는 개인 프로젝트 Snapshot이나 운영 DB를 제공하지 않습니다.
- Capability Registry와 Validation Policy는 허용 동작, 도구, 승인 조건과 재시도 상한을 명시합니다.
- Dependency Graph와 Work Selection은 작업 순서와 가능한 다음 단계를 계산합니다.
- Evidence Report는 실행 결과를 축약하고 알려진 비밀정보 패턴과 민감 필드를 마스킹합니다.
- Cross-domain Planner는 검토용 계획을 만들 뿐입니다. 승인 이후에도 명시적 Adapter가 없으면 실행하지 않습니다.
- Bounded Retry와 HOLD Recovery는 제한된 재시도, 새 검증 근거 및 수동 복구 경계를 둡니다.

## 상태 모델

확인 결과는 PASS, HOLD_AUTO, HOLD_MANUAL, FAIL_TERMINAL 또는 NEEDS_MANUAL 등 명시된 상태로 표현합니다. 입력이 불완전하거나 근거가 오래되었거나 외부 작업이 필요하면 fail-closed 방식으로 보류합니다. 어떤 런타임에서도 가능한 상태를 기본 구현이 모두 실행한다고 간주하지 않습니다.

## 확장 경계

Core는 Runtime-agnostic 계약을 목표로 하지만 현재 실행 연결은 Codex CLI Adapter로 한정됩니다. 다른 플랫폼 Adapter, 외부 서비스 쓰기, 배포, 개인 프로파일 관리 UI와 중앙 데이터베이스는 현재 저장소에 포함되지 않습니다. 개인 프로파일 설정은 로컬 JSON 파일과 CLI 검증·초기화 명령을 사용합니다.
