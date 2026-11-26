# Runtime Adapter

## 현재 검증 범위

공통 계약은 Runtime 독립을 목표로 하지만 현재 제공하는 실행 연결은 Codex CLI용 Codex Adapter입니다. 다른 Runtime을 현재 지원한다고 간주하지 않습니다.

Codex Adapter의 구성은 다음과 같습니다.

- .codex/config.toml은 멀티에이전트 기능과 Agent별 설정 파일을 등록합니다.
- .codex/agents/*.toml은 Role별 전문 지침을 제공합니다.
- .codex/hooks.json은 UserPromptSubmit 이벤트를 adapters/codex/route_agent.py에 연결합니다.
- Hook은 Registry에 근거한 라우팅 선택과 작은 세션 상태를 만들고, 공유·개인 페르소나 설정의 표현 정보를 Codex 추가 Context로 반환합니다.

공유 페르소나 설정은 저장소의 `.agents/persona-settings.json`에서 읽고 개인 설정은 저장소 밖의 사용자 설정 경로에서 읽습니다. 개인 파일은 POSIX에서 0600, 상위 디렉터리는 0700 권한이어야 합니다. 개인 설정 경로를 찾을 수 없으면 개인 설정 없이 공유 기본값을 사용합니다. 파일이 있으나 형식이나 권한이 잘못된 경우 Hook은 안전하게 차단합니다. 페르소나 적용 순서는 [Agent 설정](agent-configuration.md)에 정의되어 있습니다.

Hook이 하위 Agent 실행, 모델 선택, 플랫폼 권한 또는 독립적인 사용자 화면 발화를 강제하지는 않습니다. 실행을 입증하는 Runtime 결과가 없으면 Coordinator는 계획 또는 보류로 보고해야 합니다.

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
