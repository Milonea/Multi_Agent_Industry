# 변경 기록

## 미출시

- Project 라우트에 선택 필드 `path`를 추가했습니다. Codex 이벤트의 작업 디렉터리가 선언된 경로 안에 있으면 해당 Project를 선택하며, 명시 `project:` 토큰과 같은 범위의 기존 상태가 우선합니다. 기존 문자열 형식 라우트는 그대로 유효하고 기본 `project_routes`는 비어 있습니다.
- Codex Hook의 라우트 상태 범위를 세션 ID와 작업 디렉터리의 불투명 Hash로 바꿨습니다. 같은 세션에서 다른 디렉터리로 이동하면 이전 담당자를 이어받지 않습니다. 상대 경로 `cwd`는 차단합니다. 이전 형식의 상태 파일은 더 이상 읽지 않으며 삭제해도 됩니다.
- 라우팅 우선순위의 마지막 단계(Registry 기본 Agent Fallback)를 문서와 테스트로 명시했습니다.
- Conversation Handoff 계약에 표현 전용 선택 필드 `supporting_presentation_profile_ids`를 추가하고 기존 `active_presentation_profile_id`에 표현 전용 설명을 붙였습니다.
- Portfolio Baseline에 `retired` Role을 추가하고, 매니페스트의 `external` availability를 보고서 Schema 값(`outside_registered_scope`)으로 변환하며, `.tmp*` 제외 규칙을 측정 정책에 명시했습니다. 합성 Fixture 기반 테스트를 추가했습니다.
- Coordinator와 Agent 설정 문서에 소유권이 겹치지 않는 작업만 병렬 배정하는 규칙을 추가했습니다.
- 공유·개인 페르소나 설정에 표현 전용 `presentation_policy`(규칙, 맥락별 존재감, 대화 예산)를 추가했습니다. 보안·파괴적 작업 맥락의 존재감은 낮게 제한하며 Codex Hook은 병합된 규칙을 Context로만 전달합니다.
- 공유 기본값에는 캐릭터 중립적인 일반 표현 규칙만 두었습니다. 기존 운영 저장소의 개인 페르소나 규칙은 사용자 전용 설정 파일로만 옮겼습니다.
- 저장소 공유 페르소나 기본값과 사용자별 비공개 설정을 별도 계층으로 추가했습니다.
- 개인 프로파일 우선순위, 사용자 경로 권한 검사, CLI 초기화·검증 및 Codex Hook Context 적용을 추가했습니다.
- 개인 설정 값이나 개인 프로파일 예시는 저장소에 포함하지 않았습니다.

## 미출시 — 최초 마이그레이션 후보

- 범용 Agent Registry, Role 기반 라우팅, 최소 대화 상태 및 근거 기반 Handoff를 추가했습니다.
- Portfolio Control Plane의 업무 계약, Capability/Validation Registry, 현실 점검, 선택, 재시도, Evidence 및 보류 로직을 추가했습니다.
- Codex CLI 선택형 Hook Adapter와 합성 예제 설정을 추가했습니다.
- 공통 개발 템플릿의 운영 정책과 필수 PR 항목을 보존하고 문서를 한국어로 정리했습니다.
- 실제 개인 정보, 프로젝트 Snapshot 및 외부 서비스 자격 증명은 포함하지 않았습니다.
- 최초 마이그레이션은 사용자 검토가 필요한 Draft Pull Request로 제안됩니다.
