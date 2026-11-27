# 빠른 시작

## Core 검증

Python 3.10 이상이 설치된 환경에서 저장소 루트로 이동한 뒤 다음을 실행합니다. 패키지 설치와 외부 자격 증명은 필요하지 않습니다.

    python -m agent_runtime validate
    python -m agent_runtime validate-personas
    python -m unittest discover -s tests -v
    python scripts/validate_repo.py

첫 명령은 공통 Agent Registry의 필수 필드와 제한을 확인합니다. 페르소나 검증은 공유 설정과 기본 경로에 있는 개인 설정(파일이 있으면)을 읽습니다. 개인 설정 파일 생성은 `python -m agent_runtime init-personal-personas`로 시작할 수 있습니다. 이 명령은 빈 파일만 생성하고 기존 파일은 덮어쓰지 않습니다. 테스트는 합성 계약과 로컬 상태를 사용하며 실제 외부 서비스를 호출하지 않습니다.

## Codex CLI 설정 확인

저장소의 .codex/config.toml은 Coordinator와 필요한 전문 Agent 정의를 연결합니다. .codex/hooks.json은 Codex의 UserPromptSubmit Hook을 adapters/codex/route_agent.py에 연결합니다. Codex는 프로젝트 로컬 설정을 실행하기 전에 해당 프로젝트의 신뢰를 요구할 수 있습니다. 처음 신뢰를 허용하기 전에 설정과 Hook 코드를 검토하세요.

환경에서 Codex CLI가 지원되는지 확인하고, 프로젝트 설정에서 strict config 검사를 실행합니다. 현재 자동 검사만으로는 실제 대화 화면에서의 신뢰 승인, 하위 Agent 실행, 대화 전환 또는 발화 표시를 증명하지 못합니다. 이 검증은 Codex를 실행하는 각 환경에서 별도로 확인해야 합니다.

## Agent와 프로젝트 등록

공통 Agent는 .agents/registry.json에 등록하고 실제 Codex 실행 설정은 .codex/agents/<agent_id>.toml에 연결합니다. Registry에 에이전트만 추가했다고 Runtime이 지원하는 Agent 수나 동시 실행 수가 늘어나지는 않습니다. 두 제한을 구분하고 실제 Runtime에서 확인하세요.

프로젝트 라우트는 project_routes에 등록합니다. 작업 디렉터리 기반 선택이 필요하면 라우트를 `agent_id`와 `path`를 가진 객체로 적습니다([Agent 설정](agent-configuration.md) 참고). 초기 설정은 비어 있으며 .agents/project-routes.example.json은 합성 예제입니다. 실제 저장소 이름, 절대 경로, 개인 업무 정보는 공개 예제에 넣지 않습니다.

## 라우팅 토큰

사용자 요청에 명시 토큰을 넣을 수 있습니다.

- @agent:reviewer는 특정 Agent ID를 선택합니다.
- role:validator는 활성 Validator가 하나일 때 해당 역할을 선택합니다.
- project:sample-project는 해당 프로젝트에 연결된 Agent를 선택합니다.
- support:agent:researcher는 추가 지원 Agent를 요청합니다.

라우팅은 요청을 분류하고 Runtime에 전달할 Context를 만듭니다. 등록되지 않은 식별자는 차단됩니다. Codex가 실제로 Agent를 호출하지 않았으면 결과 계약에는 실행되지 않은 것으로 기록해야 합니다.

## 작업 결과 및 복구

업무 위임에는 버전이 지정된 Task/Result 계약을 사용하고 실제 검증 명령과 결과 HEAD를 근거로 보관합니다. 검증이 불가능하거나 재시도 상한을 넘으면 상태를 HOLD로 유지하고 복구 조건을 제시합니다. 사용자 설정이나 외부 시스템을 변경하는 일은 공통 Core의 자동 실행으로 처리하지 않습니다.
