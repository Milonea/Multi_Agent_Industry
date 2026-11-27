# Multi Agent Industry

개발, 프로젝트 관리, 조사, 검증 및 업무 자동화를 위한 범용 멀티에이전트 오케스트레이션 프레임워크입니다. 프로젝트 데이터와 실행 환경은 분리하고, 명시적인 계약과 검증 근거를 바탕으로 필요한 에이전트만 선택합니다.

페르소나나 등록된 프로젝트가 없어도 핵심 설정을 검증할 수 있습니다. 페르소나는 선택적인 표현 정보이며 업무 역할, 라우팅 또는 실행 권한을 바꾸지 않습니다.

## 핵심 기능

- 역할, 권한, Runtime Binding, 공유·개인 표현 설정을 분리한 Agent Registry
- 직접 Agent·Role·Project 및 선택적 작업 디렉터리 기반의 결정적 라우팅과 세션·디렉터리 범위의 담당자 연속성
- Task, Result, Handoff 및 Semantic Event 계약
- 검증 정책, 상태 원장, Reality Scanner, 의존성 그래프, 업무 선택 및 Evidence Report
- 재시도 상한, 검증 보류, 복구 절차와 실패 시 안전 중단
- Codex CLI용 선택형 Runtime Adapter
- 외부 쓰기 기본 차단, 명시적 승인 경계 및 합성 Fixture 기반 테스트

## 아키텍처

| 계층 | 책임 |
| --- | --- |
| Agent Runtime | 역할 등록, 라우팅, 대화 상태, Handoff 및 선택적 표현 정보 |
| Portfolio Control Plane | 업무 계약, 상태, 현실 점검, 선택, 검증 정책, 근거 및 복구 |
| Runtime Adapter | 공통 계약을 실제 에이전트 실행 환경에 연결 |
| Optional Integration | 외부 서비스 연결. 기본 설치에 자격 증명이나 외부 서비스 접근은 필요하지 않음 |

자세한 설명은 [아키텍처](docs/architecture.md)에서 확인할 수 있습니다.

## 요구 사항 및 설치

Core는 Python 3.10 이상과 Python 표준 라이브러리만 사용합니다. 개발 검증은 Python 3.12에서 수행합니다. Codex Adapter를 사용하려면 Codex CLI, Git, Python이 별도로 필요합니다. 현재 Runtime 지원 범위와 검증 제한은 [Runtime Adapter 설명](docs/runtime-adapters.md)에 기록했습니다.

저장소를 복제한 뒤 루트 폴더에서 다음을 실행합니다.

    python -m agent_runtime validate
    python -m unittest discover -s tests -v
    python scripts/validate_repo.py

프로젝트별 명령과 복구 절차는 [.codex/PROJECT_PROFILE.md](.codex/PROJECT_PROFILE.md)를 확인하세요. 설치 안내는 [빠른 시작](docs/getting-started.md)에 있습니다.

## Agent 등록과 라우팅

공통 Agent 정의는 [.agents/registry.json](.agents/registry.json)에 있습니다. 각 정의는 Agent ID, Role, Capabilities, Responsibilities, Permissions, Validation Requirements, Runtime Binding 및 Escalation Policy를 구분합니다. 등록 가능한 에이전트 수와 동시에 실행할 수 있는 수는 별도 제한입니다.

Codex 세션에서 다음 선택 토큰을 사용자 요청에 포함하면 Hook이 등록 정보를 확인하고 작은 라우팅 상태를 갱신합니다.

- @agent:reviewer — 명시적 Agent 선택
- role:researcher — 해당 Role의 Agent 선택. 같은 Role에 여러 Instance가 있으면 Agent ID가 필요함
- project:sample-project — 등록된 프로젝트 라우팅 선택
- support:agent:validator — 지원 담당자 추가

기본 프로젝트 라우트는 비어 있습니다. 실제 프로젝트 이름, 경로, 저장소와 프로젝트별 페르소나 배정은 저장소 밖의 개인 프로젝트 오버레이에만 둡니다. [오버레이 예제](.agents/project-overlay.personal.example.json)는 합성 `sample-*` 값만 포함합니다([Agent 설정](docs/agent-configuration.md#개인-프로젝트-오버레이) 참고). Hook은 담당자와 Runtime Binding을 선택해 짧은 Context를 제공합니다. 라우팅 자체는 실행이나 권한 부여가 아니며, Codex가 실제 하위 Agent를 시작했는지와 결과 근거를 따로 확인합니다.

## 업무 위임과 검증

업무 계약은 .agents/contracts와 portfolio_control_plane/contracts/schemas에 정의되어 있습니다. 작업에는 목표, 범위, 제외 범위, 입력·산출물, 실행 권한, 검증 조건, 중단 조건, 결과, 근거, 위험 및 후속 작업을 연결합니다.

검증 결과가 부족하거나 에이전트·프로젝트·Capability가 등록되지 않은 경우 시스템은 보류 또는 차단 상태로 남습니다. 승인만으로 외부 쓰기가 실행되지 않으며, 외부 Adapter가 실제로 구성되지 않으면 실행 대신 수동 확인 상태를 반환합니다.

## 보안 경계

- 기본 레지스트리는 로컬 작업과 읽기 전용 Capability만 제공합니다.
- 외부 쓰기에는 별도의 명시적 승인이 필요합니다.
- Hook은 대화 본문이나 Transcript를 저장하지 않고 Agent·Project ID만 개인 접근 권한이 제한된 로컬 임시 상태로 저장합니다.
- API Key, Token, 외부 계정 데이터, 개인 프로젝트 상태는 공개 저장소에 두지 않습니다.
- 개인 페르소나 설정은 저장소 밖의 사용자 전용 경로에 두며, Codex Hook은 이를 라우팅 상태에 저장하지 않습니다.
- Hook과 프로젝트 로컬 설정은 Codex가 프로젝트를 신뢰할 때만 활성화됩니다. 처음 실행하기 전에 파일과 명령을 검토하세요.

전체 경계는 [보안 모델](docs/security-model.md)에 기록했습니다.

## 페르소나 확장

공유 전역 페르소나는 `.agents/persona-settings.json`에서 간단한 locale, style, tone tag로 관리하고, 상세한 인물 정체성·말투·반응·관계는 사용자 홈의 개인 설정에만 저장할 수 있습니다. 개인 Agent·Role 설정과 개인 기본값이 공유 설정에 우선 적용됩니다. 어느 범위의 페르소나도 권한, Role, 프로젝트 라우트 또는 Runtime Binding을 변경할 수 없습니다. 자세한 경로, 우선순위 및 초기화 방법은 [Agent 설정](docs/agent-configuration.md)을 확인하세요.

## 저장소 개발 템플릿

이 저장소는 범용 프레임워크 코드와 함께 재사용 가능한 Codex 개발 운영 템플릿을 유지합니다.

기반 파일은 AGENTS.md, CONTRIBUTING.md, .codex/WORKFLOW.md, .codex/PR_POLICY.md, .codex/PROJECT_PROFILE.template.md, .github/pull_request_template.md 및 .github/CI_SETUP.template.md입니다.

신규 저장소에 적용할 때는 다음 순서로 진행합니다.

1. 공통 템플릿 파일을 신규 저장소에 복사합니다.
2. .codex/PROJECT_PROFILE.template.md를 .codex/PROJECT_PROFILE.md로 복사하고 실제 기술 스택, 명령, 데이터 경계, 역할 및 수동 검증을 확인해 작성합니다.
3. 실제 프로파일 명령을 확인한 뒤 Pull Request와 기본 브랜치 변경을 검증하는 GitHub Actions를 설정합니다. 외부 Action은 검토된 전체 Commit SHA로 고정하고 필요한 최소 권한만 부여합니다.
4. 기본 브랜치를 보호하고 단일 통합 검증을 필수 검사로 지정합니다. 계정 요금제와 권한이 허용하면 자동 병합을 설정합니다.
5. 사용 전에 문서 전용 Pull Request와 코드 Pull Request로 전체 흐름을 검증합니다.

기존 저장소에 적용할 때는 브랜치, 작업 트리, 원격, 열린 Pull Request와 미커밋 작업을 먼저 확인하고 기존 변경을 보존합니다.

예시 CI 정책은 실제 프로젝트 명령과 필수 검사 이름을 확정하기 전 활성화하지 않습니다. 다른 저장소의 페르소나 이름, 외부 서비스 규칙 또는 기술 가정을 복사하지 않습니다. 개발 수명주기와 병합 정책은 [AGENTS.md](AGENTS.md), [작업 흐름](.codex/WORKFLOW.md), [PR 정책](.codex/PR_POLICY.md)을 기준으로 합니다.

## 문서와 참여

- [빠른 시작](docs/getting-started.md)
- [Agent 설정](docs/agent-configuration.md)
- [Runtime Adapter](docs/runtime-adapters.md)
- [보안 모델](docs/security-model.md)
- [참여 안내](CONTRIBUTING.md)
- [변경 기록](CHANGELOG.md)
- [공개 이전 보고서](docs/migration-report.md)

이슈나 Pull Request에는 재현 방법, 검증 명령, 실제 결과, 위험 및 복구 방법을 포함하세요. 라이선스와 재배포 조건이 지정되기 전까지 이 저장소는 라이선스 미지정 상태입니다.
