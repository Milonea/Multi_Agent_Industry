# 변경 기록

## 미출시

- 저장소 밖 개인 프로젝트 오버레이(`project-overlay.json`, `MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY`)를 추가했습니다. 실제 프로젝트 경로·저장소·상태·프로젝트별 페르소나 배정은 개인 파일에만 두고, 공개 저장소에는 Schema, 로더, 합성 예제와 테스트만 둡니다. 유효 라우트는 Registry와 오버레이의 합집합이며 ID·경로 충돌은 차단합니다.
- Project 상태 `active`·`hold`·`inventory_only`를 추가했습니다. `hold`에서는 `hold_agent_id`로 라우팅하고 구현 가능 Agent를 명시 토큰·Role·기존 상태로도 거부하며, `inventory_only`는 라우팅하지 않습니다.
- 페르소나 우선순위 맨 위에 프로젝트 Agent·Role 배정 계층을 추가했습니다. 라우트의 활성 Project가 오버레이에 있을 때만 적용합니다.
- Codex Hook Context에 `project_status`와 오버레이 `constraints`를 추가했습니다. 경로·저장소·Runtime 식별자는 전달하지 않습니다.
- Project 경로 매칭과 충돌 검사를 파일 시스템 기준(inode, 없는 경로는 정규화 철자)으로 바꿨습니다. 대소문자·Unicode 정규화·별칭 철자로 `hold`를 우회할 수 없습니다.
- 개인 설정 파일을 `O_NOFOLLOW`·디렉터리 descriptor·`fstat`으로 읽어 확인과 읽기 사이의 경로 교체를 막았습니다. 파일의 직접 상위 디렉터리가 심볼릭 링크이면 거부합니다.
- 오버레이와 Registry 라우트의 오류 메시지에서 Project ID, 키와 값을 제거했습니다.
- `init-project-overlay`, `validate-project-overlay` CLI 명령을 추가했습니다. 개인 설정 파일 안전 검사를 공통 모듈(`agent_runtime/private_files.py`)로 옮겼습니다.
- `.agents/project-routes.example.json`과 그 Schema(`.agents/schemas/project.schema.json`)를 `.agents/project-overlay.personal.example.json`과 `.agents/schemas/project-overlay.schema.json`으로 대체했습니다. `scripts/validate_repo.py`가 예제의 합성 식별자와 자리표시자 경로를 검사합니다.
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
