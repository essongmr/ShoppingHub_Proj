# ShoppingHub UI V1 Baseline

## 목적
이 패키지는 Content Linker backend를 유지하면서 ShoppingHub의 실제 사용자 UI를 `/ui-preview/`에서 확정한 디자인과 UX로 복구하기 위한 기준본이다.

- Content Linker = Backend / Admin / Integration Engine
- ShoppingHub = Creator / Customer-facing Product
- backend business logic, owner isolation, DM engine, tracking은 재설계하지 않는다.
- UI는 이 패키지의 4개 reference template을 source of truth로 삼는다.

## 가장 중요한 원칙
`비슷하게 새로 디자인`하지 않는다. Preview의 geometry, hierarchy, spacing, card structure, responsive behavior를 그대로 가져오고 sample data만 실제 Django context로 교체한다.

### 실제 서비스에서 제거할 Preview 전용 요소
- UI REVIEW
- 01~15 번호 navigation
- 화면목록 / 이전 / 다음
- sample/debug 문구

## Reference files
- `reference/01-06_onboarding_source.html`: 가입부터 Store 완성까지
- `reference/07-11_creator_workspace_source.html`: Creator Home, 상품, DM, 진열, 통계
- `reference/12-15_customer_store_source.html`: Public Store, Empty, Search, Product/ProductBlock detail
- `reference/billing_source.html`: 요금제/결제

## 01~06 Onboarding UX
공통 shell을 사용한다. 화면마다 폭, 카드 크기, Store preview 크기가 달라지면 안 된다.

### 01 가입
- Welcome + 가입 card + concept visual
- 전통적인 Django 가입폼처럼 보이지 않게 한다.

### 02 Store 이름
- 왼쪽 질문 card / 오른쪽 Store Preview
- store_name만 입력
- slug/internal ID 노출 금지

### 03 대표 프로필
- Instagram/YouTube profile card 중심
- 대표 profile 하나 선택
- 직접 사진은 secondary
- 선택 즉시 Store Preview 변화

### 04 소개
- 짧은 소개 + suggestion chips
- live Store Preview

### 05 주소
- 주소 card를 먼저 보여준다.
- `다른 주소로 바꿀게요`를 선택할 때만 slug input 노출

### 06 완료
- centered completion hero
- Store preview가 화면의 주인공
- CTA: 첫 상품 진열하기 / 내 Store 보기 / 주소 복사

## 07 Creator Home
- Preview 07 geometry 그대로 사용
- 일반 SaaS/ERP dashboard 스타일 금지
- 실제 DB count만 연결
- Main nav: 홈 / 상품 / DM / 진열 / 통계
- Mobile bottom navigation 유지

## 08 상품 관리
### 기본 화면
- `+ 상품 등록` / `+ 상품블록 만들기`
- 전체 / 상품 / 상품블록 chip
- table 금지, card grid

### 단일상품 등록
중요: 한 항목 입력 후 전체 페이지가 바뀌는 wizard 금지.
하나의 page/container 안에서 card flow로 진행한다.

순서:
1. 상품번호 + 상품명
2. 대표이미지 확인/선택
3. 판매사이트 card/chip 선택
4. 상품 링크
5. 짧은 소개 optional
6. 최종 Product Card Preview

기존 session backend는 사용 가능하지만 브라우저에서는 하나의 연속된 대화형 흐름으로 보여야 한다.

### 상품블록
구성상품은 파일에서 찾지 않는다.
현재 Creator가 이미 등록한 `Linker(owner=request.user)` 상품에서 고른다.

Desktop:
- 왼쪽 `내 상품`
- 오른쪽 `B007에 담을 상품`
- click 추가 + drag/drop
- 선택된 상품끼리 drag/drop 순서 변경

Mobile:
- tap selection
- 선택/선택됨
- 위/아래 reorder 대안

대표이미지는 optional. 없으면 첫 구성상품 thumbnail fallback.

주의: 현재 backend의 `products.filter(pk__in=selected_ids)`는 POST 순서를 잃을 수 있으므로 `selected_ids` 순서대로 ProductBlockItem.sort_order를 저장해야 한다. schema 변경은 필요 없다.

## 09 Instagram DM
Creator가 기술적인 rule form을 다루는 느낌이 나면 안 된다.

순서:
1. Instagram 콘텐츠 card 선택
2. 댓글 조건 card 선택
   - 특정 댓글
   - 모든 댓글
   - 댓글별 다르게
3. 홍보 대상 card 선택
   - 상품
   - 상품블록
   - Store
4. ShoppingHub default DM 자동 생성
5. 인사말/안내문구만 수정
6. DM preview
7. 자동화 시작

`댓글별 다르게`는 Rule Card 형태:
- 007 → 상품 007
- B007 → B007 모아보기
- 전체 → Store
- + 댓글 규칙 추가

Store 경유 = default ON.
DM 안에서 복잡한 상품 carousel/판매처 비교를 하지 않는다.

## 10 진열 관리
08 상품관리와 template/view 목적을 분리한다.

Creator가 편집하는 항목:
- 공개/숨김
- category
- display order
- Creator PICK 등 진열 그룹

오른쪽(모바일에서는 아래)에 `내 Store 미리보기`가 있어야 한다.

카테고리 정책:
- `전체`만 시스템 기본
- 나머지는 Creator가 만든 category만
- 실제 공개 상품/블록이 없는 category는 Customer Store에서 숨김
- Creator 지정 순서 사용

## 11 통계
실제 StoreEvent만 사용.
- Store 방문
- 상품 조회
- 블록 조회
- 상품 확인 클릭
- 가능하면 DM→Store 경유율

가짜 매출/구매완료/전환값 금지.

## 12~15 Customer Store
Creator Workspace와 시각적으로 분리한다.
Customer Store는 이미지/발견/큐레이션 중심.

### 12 Store Home
순서:
- Creator profile / store name / tagline / SNS
- 검색: 상품번호 · 상품명 · 브랜드
- category chips (Creator 설정값만)
- Creator PICK
- 최근 소개
- 상품 모아보기
- category별 상품

가격 표시 금지.
판매처 강조 금지.
파트너 URL raw text 노출 금지.

### 13 Empty Store
Creator identity 유지 + SNS 연결.
Dead end 금지.

### 14 Search
- exact 상품번호는 exact match 우선
- 일반 keyword는 Product + ProductBlock 결과
- image card 중심

### 15 Product Detail
- 대표이미지, 번호, title, 짧은 소개
- CTA: `상품 확인하기`
- 가격/판매처명 노출 금지
- 관련상품 / 포함된 ProductBlock / Creator 추천으로 탐색 계속
- mobile sticky CTA 허용

### ProductBlock Detail
- B007 title/description
- 구성상품들을 swipe/card로 탐색
- 상품 선택 → 단일상품 detail → `상품 확인하기`

## Public deep-link
- Store: `/s/{slug}/`
- Product: `/s/{slug}/?product=007`
- ProductBlock: `/s/{slug}/?block=B007`

Store를 경유한다는 것은 Home을 강제로 먼저 보여준다는 뜻이 아니다. DM은 목적 상품/블록으로 deep-link하되 Creator Store 안에서 열린다.

## Billing
Main navigation에 넣지 않는다.
Creator profile/settings 영역의 `요금제·결제`로 둔다.
Customer Store에는 SaaS Billing UI를 표시하지 않는다.

## Codex 작업 원칙
1. 이 package의 reference를 source of truth로 사용한다.
2. backend/model/migration을 임의 확장하지 않는다.
3. UI 작업은 GPT-5.6 Luna 같은 저비용 모델 우선.
4. repo 전체 scan 금지, 필요한 template/view만 확인.
5. 화면 묶음 완료 후에만 전체 test 1회.
6. 각 화면 실제 screenshot을 Preview와 눈으로 비교한 뒤 commit.

## 권장 작업 순서
1. 01~06 geometry/flow consistency
2. 07 Creator Home
3. 08 상품/상품블록 완성
4. 09 DM
5. 10 진열
6. 11 통계
7. 12~15 Customer Store
8. 실제 Instagram 1 Cycle E2E test

