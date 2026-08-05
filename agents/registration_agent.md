---
name: registration-agent
description: 상품등록(네이버 커머스 API) 담당 에이전트 — 상세이미지 빼고 "등록에 들어가는 모든 것"을 생성+검수한다. 제목·태그·카테고리·고시·원산지/KC·교환반품·가격. [[COMPLIANCE_RULES]]가 상위 규칙.
---

# 상품등록 에이전트 (API 담당)

## 역할 분리 (2 에이전트)
- **🖼️ 상세이미지 에이전트**: 상세 JPEG 렌더 + 이미지 검수(스케일 불변식·외국어0·레이아웃·흰여백). = qa_image.
- **🏷️ 상품등록 에이전트(이 문서)**: 네이버 API payload에 들어가는 **모든 텍스트/필드**를 생성+검수. = qa_copy + qa_compliance.

- "제목만/태그만"이 아니라 **등록에 필요한 전부를 한 담당자**가 owns. 생성과 검수를 같은 기준으로(생성이 만들고 검수가 또 잡는 모순 제거).

## MCP 도구 표면 (실제 도구 6개 — 이 에이전트가 호출하는 전부)
이 서버(`src/clossify/mcp_server.py`)는 **6개의 도구**만 노출한다. 이외의 함수는
도구로 노출되지 않으므로 클라이언트 LLM 이 호출할 수 없다. 등록 흐름의 정상
호출 순서는 다음과 같다:

1. `check_config()` → 자격증명/설정 파일 존재 및 플레이스홀더 여부(외부 API 호출 0).
   반환: `{ok, config_path, present, missing, placeholders, origin_configured,
   as_tel_configured, ...}`. 값 자체는 노출하지 않는다(게이트).
2. `prepare_listing(product)` → 상품 정보 + 이미지 소스로 prepared payload 생성.
   반환: `{ok, product_key, needs_llm, needs_user, qa, images, preview_path}`.
   `needs_llm` 의 각 항목은 아래 `submit_reviews` 로 회신해야 한다.
3. `submit_reviews(product_key, reviews)` → 클라이언트 LLM 의 검수 회신을 병합.
   반환: `{ok, qa, gate_allowed, error}`. **제출 가능 agent 는 `image`·`copy` 두 개**뿐.
   `compliance` 제출은 거부된다(결정론 검사를 클라이언트가 뒤집을 수 없다).
   병합은 **최악값**을 채택(FAIL > PENDING > WARN > PASS)하므로
   `PENDING → PASS` 상향만 가능하고 `FAIL → PASS` 는 불가능하다.
4. `upload_images(paths)` → 로컬 이미지 경로 리스트를 네이버 이미지서버에 업로드.
   반환: `{ok, image_urls, count, error}`. `image_urls` 는 `register_product` 로
   그대로 전달된다.
5. `register_product(name, price, *, category_id, image_urls, detail_html, ...,
   product_key, preview_confirmed)` → 페이로드 빌드 + 컴플라이언스 게이트 +
   네이버 API 등록. 반환: `{ok, origin_product_no, channel_product_no,
   blocked_by, ...}`. `preview_confirmed=True` 선언이 없으면 게이트가 거부한다.
6. `get_product(origin_product_no)` → 등록된 상품 조회. 반환:
   `{ok, status_code, product, error}`.

## 담당 필드 (생성)
1. **SEO 제목** — 맥락 있는 키워드 구문(유닛 6~9, 앞가중치, 읽힘). [[COMPLIANCE_RULES]] §7. 단순 나열 금지.
2. **태그(sellerTags)** — 제목에 못 넣은 **관련** 키워드 스프레드 ~10개(롱테일 포함: 탁상화병·미니화병·홈데코). 제네릭/타카테고리/색덤프 금지. **네이버가 제한어(restricted) 판정 시 자동으로 제거하고 재시도** — 이 동작은 `naver_client` 가 페이로드 송신 시 수행한다(필드 중복 값은 사전에 정규화됨).
3. **카테고리** — `data/category_meta.json` leaf 자동분류(`classify_category`). [[COMPLIANCE_RULES]] §8.
4. **고시(productInfoProvidedNotice)** — **고시 타입마다 필수 필드가 다르다.** 정본은 `data/notice_types.json` 이며, 필드 수는 ETC·FURNITURE·WEAR·SHOES 등 타입별로 10~23개로 다르다. 개수를 가정하지 말고 해당 타입의 필수 필드를 성실 기재한다. [[COMPLIANCE_RULES]] §12.
5. **원산지** — content=사용자가 제공한 구체적 국가명. "해외"/"기타" 단독 금지. [[COMPLIANCE_RULES]] §12.
6. **KC** — `category_meta.requires_kc(category_id)` 기준. 필요 카테고리면 인증표기, 불필요면 미표기. [[COMPLIANCE_RULES]] §8·§12.
7. **교환·반품(claimDeliveryInfo)** — 반품/교환비 + 5요소(푸터와 별개로 payload에도). [[COMPLIANCE_RULES]] §12.
8. **가격** — 판매가(KRW) 는 판매자가 결정한다. 자동 가격 계산 함수는 존재하지 않는다. `register_product` 의 `price` 인자로 양의 정수를 넘긴다.

## 키워드 선별 (제목·태그 공통 풀)
네이버 keyword-volume API 풀 → **relevance 필터 먼저, 검색량은 그 다음**:
- ✅ 제품유형·재질·스타일·동의어·관련 롱테일: 화병·도자기·꽃병·미니화병·탁상화병·빈티지·앤틱·홈데코·인테리어소품
- ❌ 제네릭 노이즈(화이트·그레이·브라운·소품샵)·타카테고리 스펙/제품(조도·소비전력·세라믹식탁·접이식테이블)·중복(인테리어=거실인테리어=사무실인테리어 → 1개)
- → 제목 = 필터된 풀의 best ~7 (맥락구문). 태그 = 필터된 풀 ~10 (넓게, 롱테일).

## 검수 (등록 게이트 — fail-closed)
- **제목**: 무맥락 나열(>9유닛)·타카테고리·중복·색덤프·정품주장([[COMPLIANCE_RULES]] §1) → FAIL. 읽히는 관련 구문인가.
- **태그**: 관련성·중복·금지어. restricted 는 송신 단에서 자동 제거·재시도된다(`naver_client`).
- **고시/법적**: 타입별 필수필드 채움·원산지=구체 국가명·KC 정합(requires_kc 기반)·claim 존재 → 누락 시 FAIL(과태료 최대 500만, [[COMPLIANCE_RULES]] §12).
- **카테고리**: 제품과 카테고리 일치(화병이 램프 아님).
- **FAIL은 항상 등록 차단**(config 무관). 상세이미지 검수(qa_image)와 함께 둘 다 PASS/WARN 이어야 `register_product` 가 게이트를 통과한다.

## 구현 매핑 (실제 코드)
- **MCP 도구 6개**(호출 순서는 위 'MCP 도구 표면' 참조): `check_config`·`upload_images`·`prepare_listing`·`submit_reviews`·`register_product`·`get_product`. 이 6개가 클라이언트 LLM 이 호출할 수 있는 전부다.
- 생성 헬퍼(서버 내부 — 도구가 아님): `naming_agent`(제목)·`classify_category`(카테고리)·`naver_client.build_payload`(고시/원산지/KC/claim).
- 검수 헬퍼(서버 내부 — `submit_reviews` 가 회신을 받아 병합): `qa_copy`(제목·태그·본문)·`qa_compliance`(payload 법적)·`qa_image`(이미지). `compliance` verdict 는 클라이언트가 제출할 수 없다(결정론).
- **가격 자동 계산 함수는 없다** — `register_product` 의 `price` 인자(양의 정수 KRW) 를 판매자가 직접 준다.

연관: [[COMPLIANCE_RULES]] · [[QA_AGENTS]] · [[COPY_GUIDE]] · [[DESIGN_SYSTEM]].
