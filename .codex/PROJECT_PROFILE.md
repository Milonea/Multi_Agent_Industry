# 프로젝트 프로파일

## 프로젝트

- 이름: Multi Agent Industry
- 목적: 역할·권한·표현 설정을 분리하고 검증 가능한 계약으로 업무를 조정하는 범용 멀티에이전트 프레임워크.
- 기본 브랜치: main
- 지원 운영체제 또는 Runtime: Core는 Python 3.10 이상의 표준 라이브러리만 사용하도록 작성했습니다. CI에서 Linux와 Python 3.12를 검증하고, 2026-11-27 macOS와 Python 3.14에서 전체 검증 및 합성 UserPromptSubmit Hook 검사를 실행했습니다. Windows 실제 환경은 검증하지 않았습니다. Codex CLI Adapter는 별도 설치와 프로젝트 신뢰 설정이 필요합니다.

## 역할과 소유권

- Coordinator: 목표와 범위를 정리하고 담당자, 의존성, 검증, 보류 및 최종 보고를 조정합니다.
- 구현 담당자: 등록된 Agent 중 implementer 역할을 맡은 실행 주체입니다. 저장소의 작업 지침과 권한을 따릅니다.
- 선택적 전문 검토자: architect, validator, reviewer, researcher, documentation_specialist, release_manager, incident_commander, fallback_investigator 역할. 작업에 필요한 역할만 호출합니다.
- Handoff 형식: .agents/contracts와 portfolio_control_plane/contracts/schemas에 정의된 Task, Result, Lifecycle 및 Semantic Event 계약을 사용합니다. 실제 실행과 검증 근거가 확인되지 않은 결과는 완료로 승격하지 않습니다.

페르소나는 표현 방식과 분야 초점을 설명합니다. Git, 보안, 배포 또는 외부 서비스 권한을 추가하지 않습니다.

## 기술과 명령

- 언어 및 프레임워크: Python 3.10 이상, Python 표준 라이브러리. Codex CLI 선택형 Adapter.
- 설치 또는 설정 명령: 별도 패키지 설치는 필요하지 않습니다. 저장소 루트에서 python -m agent_runtime validate를 실행합니다.
- 집중 테스트 명령: python -m unittest discover -s tests -p "test_agent_runtime.py" -v 또는 python -m unittest discover -s tests -p "test_portfolio_contracts.py" -v
- 전체 필수 검증 명령: python -m unittest discover -s tests -v, python scripts/validate_repo.py, python -m agent_runtime validate
- Lint, Typecheck 또는 Build 명령: 현재 저장소에 별도 도구가 설정되어 있지 않습니다.
- 병합 후 Smoke Test: python -m agent_runtime validate와 python scripts/validate_repo.py
- 특수 하드웨어 또는 Host Software가 필요한 명령: Codex Hook을 실제 실행하려면 Codex CLI, Git, Python과 프로젝트별 Hook 신뢰 설정이 필요합니다. 실제 Codex 세션에서의 승인·호출 동작은 수동 검증 대상입니다.

## 데이터 및 부수 효과 경계

- 민감 파일 또는 데이터: 실제 사용자 프롬프트, 대화 기록, 자격 증명, 개인 프로젝트 ID와 상태는 저장소에 포함하지 않습니다.
- 커밋해서는 안 되는 생성 파일: __pycache__, *.py[cod], 가상환경, 로컬 임시 라우트 상태, 인증정보 및 운영 데이터.
- 외부 서비스 및 읽기 전용·쓰기 허용 여부: Core는 외부 서비스에 연결하지 않습니다. Codex Adapter는 로컬 라우팅만 합니다. 외부 Integration은 현재 제공하지 않습니다. 프레임워크 외부 쓰기는 별도 승인과 명시적 Adapter가 있어야 합니다.
- 비용이 발생할 수 있는 작업: Core 검증은 로컬에서 비용을 발생시키지 않습니다. 호스팅 Codex나 향후 외부 서비스를 사용할 경우 해당 Runtime의 비용 정책이 적용됩니다.
- 파괴적이거나 되돌리기 어려운 작업: Core에는 배포, 외부 쓰기, 파일 삭제 또는 서비스 변경 실행기가 없습니다. 실제 외부 작업을 추가할 때에는 별도 승인·검증·복구 계약을 먼저 정의합니다.
- Rollback 또는 복구 절차: 저장소 변경은 범위가 제한된 Commit을 revert합니다. 잘못된 로컬 대화 라우트 상태는 OS 임시 디렉터리의 multi-agent-industry-route-state 폴더에서 삭제할 수 있습니다. 상태 파일은 프롬프트나 Transcript를 포함하지 않습니다.

## 문서와 릴리스

- Changelog 또는 릴리스 노트 위치: CHANGELOG.md 및 docs/migration-report.md
- 실제로 필요한 상태 문서: 공개 기능 현황은 README와 각 기능 문서에서 관리합니다. 개인 프로젝트 상태는 이 저장소에 두지 않습니다.
- 수동 릴리스 검사: 실제 Codex CLI 세션에서 프로젝트 Hook 신뢰, 명시적 라우팅, 프로젝트 전환, 하위 에이전트 실제 실행과 Handoff 근거를 확인해야 합니다. 합성 UserPromptSubmit Hook 입력은 로컬에서 검사했지만 Codex Host 동작은 확인하지 않았습니다. Windows 실제 환경도 검증하지 않았습니다.
- 코드 병합을 차단하는 조건: 민감 데이터 검사 실패, 정책 무결성 실패, 필수 CI 실패, 불명확한 외부 쓰기 권한, 해결되지 않은 보안 경계 또는 이번 최초 공개 마이그레이션 Pull Request의 사용자 최종 검토·병합 승인 전 상태.
- 릴리스만 차단하는 조건: 실제 Codex Host에서 확인하지 않은 Hook·Agent 동작, Windows 실제 환경 미검증 또는 배포·게시 권한 미설정.
