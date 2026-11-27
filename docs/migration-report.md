# 공개 마이그레이션 보고서

## 목적

Multi Agent Industry는 프로젝트와 페르소나에 종속된 운영 설정을 포함하지 않고, 범용 멀티에이전트 업무 계약과 검증 가능한 오케스트레이션 Core를 제공하는 공개 프로젝트입니다. 이 보고서는 독립적인 공개 저장소에 반영한 최초 마이그레이션 범위와 검증 근거를 설명합니다.

## 구현된 구성

- Agent ID, Role, Capabilities, Responsibilities, Permissions, Runtime Binding 및 Escalation Policy를 분리한 Agent Registry
- 명시 Agent, Role, Project 및 세션 연속성을 지원하는 결정적 라우팅
- 실제 실행과 Evidence가 확인된 경우에만 결과를 수락하는 Handoff 경계
- Task·Result·Lifecycle·Semantic Event 및 Core 데이터 Schema
- Reality Scanner, Capability Registry, State Ledger 계약, Dependency Graph, Work Selection, Validation Policy, Evidence Report 및 bounded retry
- 승인된 외부 작업도 명시적 Adapter가 없으면 실행하지 않는 Cross-domain Planner
- Codex CLI용 선택형 UserPromptSubmit Adapter 및 역할별 사용자 정의 Agent 설정
- 별도 역할을 둔 한국어 README, 빠른 시작, 아키텍처, 설정, 보안 및 Runtime 문서

## 범용화와 데이터 경계

공개 기본 설정에는 개인 프로젝트, 사용자 대화, 운영 Snapshot, 외부 서비스 자격 증명 또는 사용자별 페르소나 원문을 포함하지 않습니다. 예제 프로젝트 라우트는 합성 데이터입니다. Persona 확장 인터페이스는 표현 정보만 받으며 권한을 변경할 수 없습니다.

외부 서비스 Integration은 현재 활성화되어 있지 않습니다. 외부 서비스 쓰기, 배포, 개인 프로파일 관리자 및 장기 데이터베이스는 지원 기능으로 제공하지 않습니다.

## 페르소나 이전

기존 운영 저장소의 페르소나는 공개 저장소에 넣지 않고 사용자 전용 개인 설정 파일로 옮겼습니다. 공개 저장소에는 스키마, 로더, 검증 코드와 빈 예제만 있습니다.

| 항목 | 이전 위치 | 상태 |
| --- | --- | --- |
| 인물 정체성·말투·반응·관계 (개인 프로파일 22개, 원본 레코드 31개) | 개인 설정 `profiles` | 완료. 2026-11-27 원본 레코드 해시와 일치 확인 |
| 기본 10개 Agent의 페르소나 배정 | 개인 설정 `assignments.agents` | 완료 |
| 호칭·이름 사용·관계 우선순위·보고 소유·안전 시 존재감 축소 규칙 | 개인 설정 `presentation_policy` | 완료 |
| 맥락별 존재감과 대화 예산 | 공유 기본값과 개인 설정 `presentation_policy` | 완료 |
| 캐릭터 중립적인 일반 표현 규칙 | 공유 `.agents/persona-settings.json` | 완료 |

원본의 프로젝트 전용 업무 Role(음악 아카이브, 금융 분석 등), 외부 서비스 Integration, 프로젝트 상태와 운영 기록은 범용 Core 범위가 아니므로 이전하지 않았습니다. 일반 Role의 책임과 권한은 이미 Agent Registry로 대체되었습니다. 실제 Codex Host에서 생성되는 답변의 말투 적용은 수동 검증 대상으로 남습니다.

## 개발 템플릿

저장소의 기존 개발 정책, 기본 브랜치 보호 원칙, 단일 통합 검증, 진짜 수동 게이트, 필수 Pull Request 항목과 리뷰 흐름을 보존했습니다. 기존 템플릿의 문서는 정책 의미를 유지하도록 한국어로 정리했습니다. 프레임워크의 업무 계약과 저장소 자체의 개발 운영 정책은 별도 문서에서 관리합니다.

## 설치 및 검증

Core는 Python 3.10 이상과 표준 라이브러리만 사용합니다. GitHub Actions는 Linux의 Python 3.12에서 실행하며, 2026-11-27에는 macOS의 Python 3.14에서도 전체 검증을 실행했습니다. 기본 검증 명령은 다음과 같습니다.

    python -m agent_runtime validate
    python -m unittest discover -s tests -v
    python scripts/validate_repo.py

현재 마이그레이션 범위에서 확인한 결과입니다.

| 검사 | 결과 |
| --- | --- |
| Python 단위·계약 테스트 | 통과, 65개 (macOS Python 3.14 로컬, 2026-11-27 페르소나 규칙 이전 기준) |
| 문서·정책·Schema 검사 | 통과, JSON 23개·TOML 11개·Markdown 링크 20개 (macOS 로컬) |
| Agent Registry 및 Runtime 권한 정합성 | 통과, Agent 10개 (macOS 로컬) |
| Codex CLI 실행 파일 | `codex-cli 0.171.0` 확인 |
| 합성 UserPromptSubmit Hook 검사 | 통과, 라우팅 Context·최소 상태 파일·잘못된 이벤트 차단 확인 |
| 실제 Codex 프로젝트 Hook 신뢰 및 대화 내 Agent 실행 | `NOT_VERIFIED` |
| Windows 실제 실행 | `NOT_VERIFIED` |

CI Workflow는 모든 Pull Request와 main 푸시에 같은 단일 통합 검증을 실행하도록 구성했습니다. 합성 Hook 검사는 실제 Codex Host의 프로젝트 신뢰, 하위 Agent 호출 또는 사용자 인터페이스 동작을 입증하지 않습니다. 해당 Host 동작과 Windows 실제 실행은 릴리스 전에 별도로 확인해야 합니다.

## 알려진 제한 및 후속 방향

- 현재 실행 Adapter는 Codex CLI에 한정됩니다.
- Hook은 Registry 기반 선택을 Context로 전달하지만 자체적으로 하위 에이전트를 실행하거나 발화자를 바꾸지 않습니다.
- 외부 쓰기 Adapter, 분산 상태 저장소, 개인 프로파일 UI 및 사용자 데이터 관리 기능은 포함하지 않습니다.
- 공개 라이선스는 지정하지 않았습니다. 재배포 조건이 확인되기 전에는 제3자 파일을 추가하지 않습니다.
- 최초 마이그레이션의 코드 검증과 실제 Runtime 검증은 별개입니다. 실제 Codex 세션과 지원되지 않은 운영체제에서의 동작을 확인한 것으로 간주하지 않습니다.
