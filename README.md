# ShoppingHub V2 clean-room starter

이 프로젝트는 `Content_Linker_Proj(1).zip`에서 **검증된 backend/domain 코드만 선별 복제**하고, 기존 production/UI preview/placeholder 화면은 복사하지 않은 1차 정리본입니다.

## 원칙
- 기존 `.env` 미포함. `.env.example`만 포함.
- Meta/Google OAuth callback path는 기존 설정을 재사용할 수 있도록 유지.
- `linker/models.py`, migrations 0001~0016, services, tasks, OAuth/credential, 멀티 Instagram, ResponseRule scope 로직은 현재 소스에서 복제.
- 예전 template/CSS/media/UI preview 문서는 복사하지 않음.
- 화면은 기능 확인용 **새 최소 UI**로 생성. 안정화 후 필요한 UI만 선택적으로 이식.
- `/workspace/`는 staff 운영자, `/creator/`는 설정, `/shoppinghub/`는 업무라는 역할을 유지.

## 처음 실행
1. `.env.example`을 참고해 `.env` 생성 (기존 프로젝트의 secret 값은 사용자가 직접 옮김).
2. 기존 프로젝트를 중지한 뒤 이 프로젝트를 실행. 같은 nginx alias `content-linker-web`을 사용하므로 동시에 올리지 않는 것을 권장.
3. `docker compose up -d --build`
4. `docker compose exec web python manage.py migrate`
5. `docker compose exec web python manage.py createsuperuser`
6. `/signup/`부터 신규 E2E 수행.

## 주의
새 DB volume 이름은 `shoppinghub_v2_pgdata`입니다. 기존 Content Linker DB를 자동 재사용하지 않습니다. 기존 데이터가 필요하면 안정화 후 명시적으로 import합니다.
