# 아키텍처 (ARCHITECTURE)

이 문서는 Clossify 저장소의 **현재 상태**를 코드 대조 기준으로 서술한다. 미구현
구간은 명시적으로 표시한다. 과대 표기 없음.

## 1. 모듈 의존 방향

의존은 아래 방향으로만 흐른다(상위 → 하위). 역방향 import 금지.

```text
                         mcp_server  (MCP 도구, 최상위 어댑터)
                              |
        +----------+----------+----------+----------+
        |          |          |          |          |
    register   qa_agents   images    agent_calls  seo
        |          |          |
   qa_agents   category_meta  naver_client
        |          |
   naver_client  common
   category_meta
   common
        |
   common   text_props   copywriting   templates   keyword_volume
   (어디서든 import 가능한 최하위 유틸/데이터 조회)
```

### 모듈 한 줄 역할

| 모듈 | 역할 |
|------|------|
| `mcp_server` | stdio MCP 서버. 6개 도구 노출(`check_config`, `upload_images`, `register_product`, `get_product`, `prepare_listing`, `submit_reviews`). 검증 sanitization, 컴플라이언스 게이트 회선 |
| `naver_client` | 네이버 커머스 API 인증·페이로드 빌드·등록·조회·이미지 업로드 |
| `images` | 이미지 입력 정규화. 로컬 가드(`validate_local_image`), SSRF 방어 외부 URL fetch(`fetch_external_image`), 통합 진입점(`attach_images`) |
| `qa_agents` | 3분할 QA(이미지/카피/컴플라이언스) 결정론 검사 + 집계 + 등록 게이트(`qa_gate`) |
| `register` | prepared payload 저장/로드, 등록 오케스트레이션. `prepare_listing`이 `detail_render.render_detail_html` 로 상세 HTML 을 조립한다. 이미지 입력은 `images.attach_images` 로 외부 URL fetch( SSRF 가드 적용) 와 로컬 파일 업로드를 수행한다 |
| `agent_calls` | 클라이언트 LLM 위임 디스크립터(llm_hint) 생성(naming, qa_copy) |
| `category_meta` | `data/category_meta.json` 로더. KC 필요 여부·예외 플래그·경로 조회 |
| `category` | 카테고리 상위 모듈(qa_agents/register 의존) |
| `common` | config 로더, 경로 상수, JSON 입출력 유틸. 최하위 |
| `text_props` | 금지 표현 정규식(`BANNED_CLAIM_RE`) 등 텍스트 속성 |
| `copywriting` | 카피 생성 보조 |
| `templates` | 상세페이지 HTML 템플릿 빌더(`build_korean_detail_html`) |
| `seo` | SEO 상품명/태그 |
| `keyword_volume` | 검색광고 키워드 볼륨 |

## 2. 데이터 자산 3종

모두 `data/` 아래. 공개 메타데이터이며 계정 식별자·토큰·스토어명·한자 0건.

### `category_meta.json`
- **출처**: 네이버 커머스 API(`GET /external/v1/categories`, 리프 카테고리 상세).
- **갱신**: `python scripts/fetch_category_meta.py`(수동, 재개 가능, GET 만 호출).
- **범위**: 리프(`last=true`) 카테고리 전체(약 4,999건). `exceptionalCategories`에
  `KC_CERTIFICATION` 포함 시 KC 대상.
- **구조**: `{generated_at, source, count, categories:[{id,name,wholeCategoryName,last,exceptionalCategories}]}`.
- **커버리지(중요)**: 상세 조회가 실패해 `exceptionalCategories` 를 확정하지 못한
  카테고리 91건이 최상위 `incomplete.ids` 에 별도 키로 명시된다(사유·건수 포함).
  이 ID 들은 `exceptionalCategories: []` 로 남아 있어 비-KC 카테고리와 동일하게
  보이나, `requires_kc()` 가 **불명(None)** 을 반환해 컴플라이언스 게이트가
  fail-closed 차단한다. 자세한 내용은 `data/README.md` 와 아래 '6. 카테고리 메타
  KC 판정 3-상태' 절을 본다.

### `certification_types.json`
- **출처**: 동일 API의 `certificationInfos`. 인증 타입 마스터(57종). 모든 카테고리
  공통이라 1회만 저장(용량 절약).

### `notice_types.json`
- **출처**: 네이버 커머스 API 공식 문서(`productInfoProvidedNoticeType` enum 정의)의
  공개 조사. **쓰기 API 호출 없음**.
- **범위**: verified 35종 / unverified 5종.
- **핵심 구조(중요)**: 각 타입은 **공통 필드 5개 + 타입별 고유 필드** 로 갈라진다.
  데이터의 `node` 키가 타입별 하위 노드 이름(`etc`/`furniture`/`wear`/...)을 가리키고,
  `fields` 가 필수 필드 목록이다. 컴플라이언스 검사는 이 `fields` 를 기준으로
  누락을 지적한다.

조회 헬퍼는 `category_meta.py`(`load_category_meta`/`requires_kc`/`exceptional_flags`/
`category_path`), `qa_agents._load_notice_types`/`_notice_type_spec`/`_infer_notice_type`.

## 3. 등록 흐름

```text
사용자 입력(사진 + 가격/카테고리/고시 일부)
   |
   v
이미지 정규화(images.attach_images) ── 로컬 가드 / 외부 URL SSRF 가드 / CDN URL 통과
   |
   v
상세페이지 렌더(detail_render.render_detail_html) ── register.prepare_listing 이 호출.
   hero/intro/specs/options/notice 섹션을 조립해 HTML 문서를 만든다.
   |
   v
컴플라이언스 결정론 검사(qa_agents._compliance_code_check)
   ├─ 고시 필수 필드(data/notice_types.json 기반, 타입별 node/fields)
   │  (단, "상세참조"·"상세페이지 참조"·"해당없음"·"-" 등 안내문구 토큰은
   │   미제공으로 간주해 FAIL. payload 전송은 그대로 하되 판정은 유효제공으로
   │   보지 않는다 — 전송과 판정을 분리.)
   ├─ 원산지(config 값과 payload 값 일치, 빈 값 FAIL)
   ├─ KC(category_meta.requires_kc() 가 True 인데 정보 없으면 FAIL;
   │  KC 필요 여부가 불명(incomplete.ids) 이면 FAIL — fail-closed)
   └─ AS 전화번호(빈 값 FAIL — 코드가 임의값을 만들지 않으므로 판매자가 설정해야 함)
   |
   v
[도구 회선 게이트] register_product 내 _run_compliance_gate
   ├─ 결정론 위반(FAIL) → 네이버 API 호출 없이 거부.
   ├─ prepared payload 가 존재하면 추가로 qa_agents.qa_gate(집계 게이트) 로
   │  전체 FAIL·PENDING 차단 여부를 확인한다(게이트 label 이 "full").
   └─ prepared 가 없으면 결정론-only("deterministic_only"). LLM 판단 미회신은
      pending_reviews 로 응답에 표기만 한다(집계 게이트보다 약한 정책).
   |
   v
네이버 커머스 등록(naver_client.register_product)
   |
   v
등록 후 재검증(naver_client.get_product)  ◀── register_prepared_listing 경로에 존재
```

> 사용자가 직접 `detail_html` 인자를 `register_product`에 전달하면 렌더 단계를
> 건너뛴다. `prepare_listing` 도구를 거치면 `detail_render.render_detail_html` 이
> 자동 조립한 HTML 이 prepared payload 에 저장되고, 이후 `register_product` 가
> 그 값을 사용한다.

## 4. QA verdict 체계

4개 verdict 와 게이트 규칙.

| verdict | 의미 | 게이트 |
|---------|------|--------|
| `PASS` | 합격 | 통과 |
| `WARN` | 주의(결정론적 경고) | 통과 |
| `PENDING` | 판단이 이뤄진 **증거가 없음**. LLM 위임 회신 대기/미접합 | **차단** |
| `FAIL` | 결정론적 위반(금지 표현/고시 필수 누락/원산지·KC 위반/AS 연락처 누락/KC 필요 여부 불명) | **차단** |

**두 게이트 레이어 주의(정확한 구분)**:

- **집계 게이트**(`qa_agents.qa_gate`): `FAIL`·`PENDING` 차단, `WARN`·`PASS` 통과.
  위임 미회신(PENDING)을 등록으로 넘기지 않는다(ADR-0002 "클라이언트 LLM이 관대해도
  서버가 막는다"). `register_prepared_listing`이 항상 호출.
- **도구 회선 게이트**(`mcp_server.register_product`의 `_run_compliance_gate`):
  두 가지 경로가 있다(응답의 `gate` 필드로 구분).
  - `deterministic_only` — prepared payload 가 없을 때. `qa_agents._compliance_code_check`
    만 호출해 결정론 위반(`FAIL`)을 차단한다. LLM 판단(카피/이미지 QA)은 위임 왕복
    연결 전이라 `pending_reviews` 리스트로 응답에 표기만 한다.
  - `full` — 동일 product_key 의 prepared payload 가 존재할 때. 추가로
    `qa_agents.qa_gate`(집계 게이트)를 호출해 `FAIL`·`PENDING` 모두 차단한다.
    집계 게이트와 동일 강도.

**핵심 원칙**: 판단이 이뤄졌다는 증거(`verdict` 키, 정규화된 값)가 없으면 `PENDING`.
`_normalize_qa_result`는 `verdict` 누락·위임 디스크립터(llm_hint)가 결과 자리에 있는
경우를 모두 PENDING으로 판정한다(fail-open 차단).

## 5. 실전 함정

등록 시 자주 걸리는 실전 사항들.

1. **판매중지 직접 지정 불가** — `register_product`의 `status`는 `SALE`(판매중) 또는
   `SUSPENSION`(판매중지). 네이버 정책상 등록과 동시에 특정 판매중지 상태를 직접
   지정할 수 없는 경로가 있으므로, `SALE` 로 등록 후 상태변경으로 처리해야 한다.
2. **AS 전화번호 실값 필수** — 안내문구/플레이스홀더(`{AS_TEL}` 등)를 넣으면
   등록/검증에서 거부된다. 실번호여야 한다.
3. **IP 화이트리스트** — 네이버 커머스 API는 호출 IP 화이트리스트를 요구한다.
   등록 스크립트를 실행할 PC의 공인 IP가 화이트리스트에 없으면 인증 단계에서
   거부된다.
4. **고시 타입과 하위 노드명 일치** — `productInfoProvidedNoticeType`(예: `WEAR`)과
   payload의 하위 노드 키(예: `wear`)가 일치해야 한다. `_build_compliance_context`가
   카테고리 경로에서 타입을 추론해 보정하되, 호출자가 틀린 노드명을 주면 검사가
   빈 필드로 판정될 수 있다.
5. **데이터·규칙은 스냅샷** — `data/` 카테고리/고시 메타와 `agents/COMPLIANCE_RULES.md`
   규칙은 특정 시점 기준이며 플랫폼 정책 변경 시 갱신 대상이다. 기준일은
   `data/README.md` 와 각 데이터 파일의 `generated_at` 을 본다.
6. **고시값은 코드가 만들지 않는다** — 원산지·AS 연락처·제조사·수입사·공통 5필드
   모두 판매자가 config 또는 상품 입력으로 제공한 값만 payload 에 싣는다. 과거
   버전의 안내문구 자동 채움("상세페이지 참조", "해당없음 / KC면제", "해외구매대행"
   등)은 제거됐다. 값이 없으면 빈 문자열이거나 필드 생략이고, 컴플라이언스 게이트가
   누락을 FAIL 로 차단한다.
7. **상세참조 토큰은 전송 O, 판정 X** — 사용자가 명시적으로 준 "상세참조"·
   "상세페이지 참조"·"해당없음" 등의 값은 payload 에 그대로 실려 네이버로 전송되지만,
   컴플라이언스 판정에서는 미제공으로 간주해 FAIL 지적한다. 전송과 판정을 분리한
   것이다. (`qa_agents._notice_field_missing` 의 `EMPTY_TOKENS` 집합이 이 토큰들을
   관리한다.)

## 6. 카테고리 메타 KC 판정 3-상태

`category_meta.requires_kc(category_id)` 반환값이 3 종류다. KC 인증 필요 여부를
확정할 수 없는 카테고리를 "불명" 상태로 분리해, 면제로 오판하는 허위 신고를 막는다.

| 반환 | 조건 | 컴플라이언스 게이트 동작 |
|------|------|------------------------|
| `True` | `exceptionalCategories` 에 `KC_CERTIFICATION` 포함. | KC 선언 정보 없으면 FAIL 차단. |
| `False` | `exceptionalCategories` 가 확정됐고 `KC_CERTIFICATION` 미포함. | KC 검사 생략(확정 비대상). |
| `None` | `data/category_meta.json` 의 `incomplete.ids` 에 속한 카테고리. 상세 조회 실패(429) 로 `exceptionalCategories` 를 확정하지 못함(91건). | **FAIL 차단**(불명). 실제 KC 대상인지 확인되기 전까지 등록을 진행하지 않는다(fail-closed). |

기본(`raise_if_incomplete=True`)은 `None` 대신 `IncompleteCategoryError` 를 발생시킨다.
컴플라이언스 게이트는 `raise_if_incomplete=False` 로 호출해 `None` 을 받고, 이를
"KC 필요 여부 불명" FAIL 위반으로 처리한다. `incomplete.ids` 에 속한 카테고리를
등록하려면 `data/category_meta.json` 을 갱신해 상세 조회를 완료하거나(권장),
해당 ID 의 `exceptionalCategories` 를 확정해 `incomplete.ids` 에서 제거해야 한다.
