# 보안 모델

## 신뢰 경계

사용자 대화, 로컬 작업 공간, Agent Registry, Runtime Binding, 외부 서비스 및 GitHub 저장소는 별도 신뢰 경계입니다. 공개 Registry에 등록된 역할이나 Persona가 신뢰 경계를 바꾸거나 권한을 추가할 수 없습니다.

## 최소 권한

Registry는 읽기·검토 및 필요한 로컬 Workspace 작업만 선언합니다. Codex Runtime은 Implementer와 Documentation Specialist만 workspace-write를 사용하고 나머지 역할은 read-only로 제한합니다. 외부 쓰기, 배포 및 서비스 운영 권한은 기본적으로 활성화되지 않습니다. Capability Registry도 외부 쓰기 Capability에 명시적인 승인을 요구합니다. 승인만으로 실행되는 Adapter가 생기지는 않습니다.

## 라우팅 상태

Codex Hook은 프롬프트 본문이나 Transcript를 파일로 저장하지 않습니다. 상태 파일에는 버전, 활성 Agent ID, Project ID 및 지원 Agent ID만 포함됩니다. 파일 이름은 세션 ID와 해석된 작업 디렉터리를 함께 SHA-256으로 Hash한 값이며 세션 ID나 경로 자체는 파일 이름과 내용에 남지 않습니다. 같은 세션이라도 작업 디렉터리가 바뀌면 새 범위로 시작하므로 다른 디렉터리의 담당자 연속성이 새지 않습니다. 상대 경로나 누락된 작업 디렉터리는 차단합니다. 상태 파일은 OS 임시 폴더의 전용 하위 경로에 기록합니다. POSIX에서는 디렉터리 0700, 파일 0600 권한을 확인하고 심볼릭 링크를 거부합니다.

작업 종료 후 불필요한 상태 폴더는 삭제할 수 있습니다. 이 상태는 대화의 기록이나 백업이 아닙니다. Windows ACL 권한은 런타임에서 별도 확인하지 않으므로 공유 계정이나 다중 사용자 호스트에서 Hook을 신뢰하기 전에 검증이 필요합니다.

## 페르소나 설정

공유 기본값은 저장소의 `.agents/persona-settings.json`에서 관리합니다. 개인 설정은 OS 사용자 설정 디렉터리에 두며, 경로를 바꿀 때는 `MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS`에 절대 경로를 지정합니다. 런타임은 개인 설정 경로가 저장소 안을 가리키거나 설정 파일이 심볼릭 링크인 경우 거부합니다. POSIX에서는 파일과 상위 개인 설정 디렉터리가 소유자 전용 권한이어야 합니다. Windows ACL은 코드에서 검사하지 않습니다.

공유 전역 프로파일은 `profile_id`, `locale`, `style`, `tone_tags`만 허용합니다. 개인 프로파일은 여기에 상세 인물 데이터와 해시 기반 원본 정보를 더할 수 있습니다. 선택된 인물 데이터만 Codex 추가 Context로 전달하며 원본 경로·해시는 전달하지 않고 라우팅 상태 파일에도 저장하지 않습니다. 페르소나 값으로 권한, 역할, 경로, Capabilities, Runtime Binding 또는 명령을 변경할 수 없습니다. 개인 설정 내용과 실제 사용자 값은 저장소나 CI fixture에 넣지 않습니다. `presentation_policy` 규칙도 표현 전용이며, 보안과 파괴적 작업 맥락의 페르소나 존재감은 `medium_low`를 넘도록 설정할 수 없습니다.

## 개인 프로젝트 오버레이

실제 프로젝트 이름, 경로, 저장소, 프로젝트별 페르소나 배정과 Runtime 식별자는 저장소 밖의 개인 `project-overlay.json`에만 둡니다. 공개 Registry의 `project_routes`는 비어 있고, 공개 예제는 합성 `sample-*` 값과 `/path/to/` 자리표시자만 사용하며 `scripts/validate_repo.py`가 이를 검사합니다. 파일은 개인 페르소나 설정과 같은 규칙(저장소 밖, 심볼릭 링크 금지, POSIX 0600/0700, 현재 사용자 소유)을 따르고, 검증 실패 시 내용을 출력하지 않고 차단합니다.

오버레이는 등록된 Agent를 선택하고 작업을 좁힐 수만 있습니다. 권한·실행 키는 위치와 관계없이 거부하고, `constraints`는 제한 문장으로만 전달합니다. `hold` Project에서는 구현 가능 Agent를 명시 토큰이나 기존 상태로도 선택할 수 없습니다. Hook Context에는 Project ID, 상태와 constraints만 들어가며 `path`, `worktree_roots`, `repository`, Orca 식별자와 페르소나 출처는 전달하지 않습니다. 라우트 상태 파일은 계속 ID만 저장합니다. `validation`과 `runtime` 값은 저장·검증만 하고 실행하지 않습니다.

## 입력과 결과

Registry와 상태 파일은 엄격한 필드 검증을 거칩니다. Project 라우트의 선택 `path`는 라우팅 선택에만 쓰이며 권한이나 파일 접근 범위를 넓히지 않습니다. 프롬프트의 자유 텍스트 이름·별칭으로 라우트를 추정하지 않으므로 대화 내용만으로 담당자가 바뀌지 않습니다. Conversation Handoff 계약의 `active_presentation_profile_id`와 `supporting_presentation_profile_ids`는 표현 전용 선택 필드이며 권한이나 라우팅을 결정하지 않습니다. 미등록 Agent, 비활성 Binding, 알 수 없는 프로젝트, 권한 초과 요청 및 불완전한 Handoff 결과는 차단됩니다. Evidence Report는 알려진 Credential 패턴 및 민감 필드 이름을 마스킹하지만 일반 데이터 유출을 완벽히 탐지한다고 보장하지 않습니다. 공개 전에 파일을 별도로 검토해야 합니다.

## 외부 Integration

현재 공개 Core에는 실제 외부 서비스 Adapter가 포함되어 있지 않습니다. 향후 연동은 별도 모듈로 두고 선택적 의존성, Mock/합성 Fixture, Dry-run, Allowlist, 인증·서명 확인, 멱등성, 재시도 제한, 쓰기 승인 및 복구 전략을 구현해야 합니다.

## 저장소 운영

- 개인 데이터, 실제 로그, 로컬 절대 경로, 비밀정보와 운영 Snapshot을 커밋하지 않습니다.
- 공개 Pull Request에서는 외부 Fork 코드에 쓰기 권한이나 저장소 비밀을 노출하지 않습니다.
- CI는 읽기 전용 권한을 사용하며 외부 Action을 검토된 Commit SHA에 고정합니다.
- 정책·권한·CI 변경은 정책 무결성 검사에 포함합니다.
- 보안 문제를 공개 Issue에 비밀이나 재현용 자격 증명과 함께 올리지 않습니다.
