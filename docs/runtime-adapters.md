# Runtime Adapter

## Orca 계획 및 수집

수동 게이트는 spec의 전용 섹션에 기록합니다. 현재 delegation-request 계약에는 수동 게이트 전용 필드가 없으므로 요청 JSON에는 추가하지 않습니다. Report 예제의 교체 마커가 어디든 남은 결과는 수집 시 잘못된 보고서로 처리합니다.

`python -m agent_runtime dispatch-plan --project sample-project --role implementer --objective-file objective.txt --task-id 20261128-sample-task`는 요청 계약, Task spec, 제안 argv를 JSON으로 출력합니다. Orca를 호출하지 않습니다. 실행은 orchestration 스킬을 가진 Coordinator가 담당합니다. 실제 Host 연동은 미검증입니다.

선택 옵션은 `--agent`, `--orca-repo-id`, `--output-dir`입니다. 출력 디렉터리는 저장소 밖이어야 하며 POSIX에서 0700/0600을 적용합니다. delegation-request.json, task-spec.txt, dispatch-plan.json을 생성하며 덮어쓰지 않습니다. stdout과 저장된 계획의 argv는 동일한 실제 spec 경로를 포함합니다. 세 파일 생성은 트랜잭션이 아니므로 실패하면 일부 파일이 남을 수 있으며 새 출력 디렉터리로 재시도해야 합니다. Coordinator는 `orca project setups`로 Repo ID를 확인합니다. overlay Orca 프로젝트 ID는 Repo ID가 아닙니다. existing Workspace는 기존 Worktree selector를 채워야 합니다.

`python -m agent_runtime ingest-orca-result --request delegation-request.json --worker-done worker_done.json --report delegation-result.json`는 결과와 Handoff 수락 여부를 출력합니다. reportPath는 자동으로 열지 않습니다. Orca taskId와 MAI task_id는 별도 식별자입니다. report의 식별자와 scope는 요청과 같아야 합니다.

| 입력 | 결과 |
| --- | --- |
| succeeded + 유효 report | report 상태 |
| succeeded + 없거나 잘못된 report | partial |
| failed | failed |
| stopReason lost/exited/unverifiable | blocked |

Coordinator가 receipt의 `runtime_executed`를 boolean true로 명시해야 실행으로 인정합니다(기본 false). Dispatch 추적 근거는 작업 근거를 대체하지 않으며 추적 ID만 있는 결과는 수락하지 않습니다. 근거 없는 completed report는 partial로 낮추고 Handoff에서 거부합니다. holding은 Handoff에서 blocked로 전달합니다. receipt body는 요약에 복사하지 않습니다. spec 헤더는 프로젝트와 담당 Agent를 함께 명시하므로 Worker Hook이 계획된 담당자를 선택합니다.

## 현재 검증 범위

공통 계약은 Runtime 독립을 목표로 하지만 현재 제공하는 실행 연결은 Codex CLI용 Codex Adapter입니다. 다른 Runtime을 현재 지원한다고 간주하지 않습니다.

Codex Adapter의 구성은 다음과 같습니다.

- .codex/config.toml은 멀티에이전트 기능과 Agent별 설정 파일을 등록합니다.
- .codex/agents/*.toml은 Role별 전문 지침을 제공합니다.
- .codex/hooks.json은 UserPromptSubmit 이벤트를 adapters/codex/route_agent.py에 연결합니다.
- Hook은 Registry에 근거한 라우팅 선택과 작은 세션 상태를 만들고, 공유·개인 페르소나 설정의 표현 정보를 Codex 추가 Context로 반환합니다.
- Hook은 저장소 밖의 개인 프로젝트 오버레이를 읽어 Registry 라우트와 합치고(없으면 빈 오버레이, 잘못되면 차단), Context에 `project_id`, `project_status`와 오버레이 `constraints`만 추가합니다. 경로, 저장소, Runtime 식별자와 페르소나 출처는 전달하지 않습니다. 오버레이의 `runtime` 블록(Orca 공급자, Agent 종류, Workspace 정책)은 현재 저장·검증만 하며 이 Adapter가 사용하지 않습니다.
- Hook은 이벤트의 절대 `cwd`로 경로가 선언된 Project 라우트(오버레이 `worktree_roots` 포함)를 찾고, 라우트 상태를 세션과 작업 디렉터리 범위로 분리합니다. 실제 Codex Host가 전달하는 `cwd` 값과 디렉터리 이동 시 동작은 수동 확인 대상입니다.

공유 페르소나 설정은 저장소의 `.agents/persona-settings.json`에서 간단한 표현 기본값을 읽고 개인 설정은 저장소 밖의 사용자 설정 경로에서 읽습니다. 상세한 인물 정보와 관계는 개인 설정에서만 전달하며 원본 경로·해시는 모델 Context에 포함하지 않습니다. 개인 파일은 POSIX에서 0600, 상위 디렉터리는 0700 권한이어야 합니다. 개인 설정 경로를 찾을 수 없으면 개인 설정 없이 공유 기본값을 사용합니다. 파일이 있으나 형식이나 권한이 잘못된 경우 Hook은 안전하게 차단합니다. 페르소나 적용 순서는 [Agent 설정](agent-configuration.md)에 정의되어 있습니다.

Hook이 하위 Agent 실행, 모델 선택, 플랫폼 권한 또는 독립적인 사용자 화면 발화를 강제하지는 않습니다. 실행을 입증하는 Runtime 결과가 없으면 Coordinator는 계획 또는 보류로 보고해야 합니다.

### UserPromptSubmit 라우팅 경고와 차단

Hook은 프롬프트 토큰의 형식·해석 문제와 권한·안전 문제를 구분합니다. 프롬프트는 공백과 `,`·`;` 구분자로 한 번만 나누며, 각 조각에서 처음 나온 토큰 접두사 하나만 토큰으로 읽고 나머지는 그 값으로 취급합니다. 따라서 한 토큰 값 안의 글자는 어떤 종류의 다른 토큰으로도 해석되지 않습니다. ASCII가 아닌 값은 대소문자 변환 전에 형식 오류로 거부하고(유니코드 소문자 변환이 KELVIN SIGN 같은 유사 문자를 ASCII로 바꾸기 때문), ASCII 값만 소문자로 정규화합니다. 종류별로 처음 64개의 서로 다른 값만 선택에 사용하고 초과분은 경고 후 무시하지만, hold 검사는 등록된 모든 값을 봅니다. 형식이 잘못됐거나 해석할 수 없는 토큰을 먼저 개별적으로 제외한 뒤, 같은 선택 종류에 해석 가능한 값이 둘 이상 남으면 그 종류 전체를 무시합니다. 따라서 잘못된 토큰 옆의 유효한 토큰은 계속 적용되며, hold Project의 구현 가능 Agent 요청을 가리지 못합니다.

| 분류 | 조건 | Hook 결과 |
| --- | --- | --- |
| 경고 | Agent·Role·Project 선택 값이 식별자 형식이 아니거나 ASCII가 아님 | 해당 토큰만 무시하고 토큰이 없던 것과 같은 라우트로 계속함 |
| 경고 | 한 종류에 서로 다른 값이 64개를 넘음 | 초과분을 선택에서 제외함 |
| 경고 | 같은 선택 종류에 서로 다른 유효·해석 가능 값이 둘 이상 남음 (hold 대상에서 그중 구현 가능 Agent가 없을 때) | 해당 선택 종류 전체를 무시함 |
| 경고 | Agent가 미등록이거나 Runtime Binding이 비활성·불완전함 | 해당 Agent 선택 토큰만 무시함 |
| 경고 | Project가 미등록이거나 명시한 Project가 `inventory_only`임 | 해당 Project 선택 토큰만 무시함 |
| 경고 | Role에 활성 Agent가 없거나 여러 활성 Agent가 연결됨 | 해당 Role 선택 토큰만 무시함 |
| 경고 | 지원 Agent가 잘못됐거나 사용할 수 없거나 중복이거나 활성 Agent와 같음 | 잘못된 지원 항목만 제거하고 나머지는 유지함 |
| 차단 | hold Project에서 명시 Agent·Role·지원 Agent·연속성 또는 작업 디렉터리 라우트가 구현 가능 Agent에 도달함 | 프롬프트 전체 차단 |
| 차단 | hold 대상에서 상충하거나 모호해 무시될 Agent·Role·Project 그룹 안의 유효·해석 가능 값 하나라도 구현 가능 Agent를 가리킴 | 프롬프트 전체 차단 |
| 차단 | hold 대상에서 등록됐지만 Binding이 비활성·불완전한 구현 가능 Agent를 명시 Agent·Role·지원 Agent로 지정 | 프롬프트 전체 차단 (active 대상에서는 경고 후 무시) |
| 차단 | Registry·프로젝트 오버레이·페르소나·라우트 상태가 잘못됐거나 안전하지 않음 | 프롬프트 전체 차단 |
| 차단 | 작업 디렉터리 identity 해석 실패·동일 깊이 중복 매칭 또는 Hook 입력 형식 오류 | 프롬프트 전체 차단 |

경고는 Codex CLI 0.172.0에서 확인한 `hookSpecificOutput.additionalContext`에 고정된 `Routing warning` 문장으로만 추가합니다. 프롬프트, 토큰 값, 경로, 저장소 이름과 개인 식별자는 포함하지 않습니다. `dispatch-plan`은 프롬프트 토큰을 파싱하지 않으므로 이 동작 변경의 영향을 받지 않습니다.

## 프로젝트 신뢰 및 운영체제

프로젝트 로컬 Hook은 Codex에서 프로젝트 신뢰가 설정된 뒤에만 실행될 수 있습니다. 신뢰 확인 전에 .codex 설정과 Hook 코드를 직접 검토하세요.

Core 코드는 표준 Python 경로 API를 사용합니다. 저장소의 검증 이력과 OS 지원 범위는 `.codex/PROJECT_PROFILE.md`를 기준으로 합니다. Hook은 POSIX 환경에서 임시 상태 경로, 라우팅 상태 및 개인 페르소나 파일 모드를 확인합니다. Windows ACL은 이 코드에서 직접 검사하지 않습니다.

## Adapter 계약

향후 Adapter는 Core 계약을 그대로 소비하고 다음 경계를 지켜야 합니다.

- 외부 쓰기는 기본 비활성화하고 명시적 실행 승인과 별도 권한을 확인합니다.
- 실제 Runtime 실행 여부와 실행 ID를 결과 계약에 전달합니다.
- 미실행 Agent를 실행된 것처럼 표시하지 않습니다.
- 검증 실패, Timeout, 부분 완료 및 수동 게이트를 명확히 구분합니다.
- 대화 Transcript와 비밀정보를 공통 상태 저장소에 기록하지 않습니다.
- 로컬 또는 합성 Fixture로 검증할 수 있도록 실제 외부 서비스 호출을 분리합니다.

다른 플랫폼 연결은 현재 구현되어 있지 않습니다. 새 Adapter는 요구사항, 권한, 상태 모델과 테스트 방법을 별도로 설계한 뒤 추가해야 합니다.
