# data/ — 카테고리 메타데이터

이 디렉터리는 네이버 커머스 API 에서 수집한 **카테고리 메타데이터**를
담는다. 카테고리 메타는 공개 정보이며, 계정 식별자·스토어명·토큰은
포함되어 있지 않다.

## 데이터 기준일

각 데이터 파일의 최상단 `generated_at` 값이 해당 데이터의 기준일이다.

- `category_meta.json` — 기준일 **2026-08-02T07:54:50Z**
- `notice_types.json` — 기준일 **2026-08-02T07:22:10Z**
- `notice_field_labels.json` — 기준일 **2026-08-04T00:00:00Z**

플랫폼이 카테고리·고시 규격을 변경하면 이 데이터는 낡는다. 갱신 방법은
`scripts/fetch_category_meta.py` 다.

## 파일

### `category_meta.json`

- **출처**: 네이버 커머스 API
  - `GET /external/v1/categories` (전체 목록)
  - `GET /external/v1/categories/{id}` (리프 카테고리 상세)
- **구조**:
  ```json
  {
    "generated_at": "<ISO8601 UTC>",
    "source": "commerce categories API",
    "count": 4999,
    "categories": [
      {
        "id": "50021299",
        "name": "반팔티셔츠",
        "wholeCategoryName": "패션의류>여성의류>티셔츠>반팔티셔츠",
        "last": true,
        "exceptionalCategories": ["GROUP_PRODUCT_MAX", "SAFE_CRITERION", "..."]
      }
    ]
  }
  ```
- **범위**: 리프(`last=true`) 카테고리 전체. 비-리프(중분류/대분류)는
  포함되지 않는다 — 상품 등록은 리프 카테고리에만 가능하기 때문.
- **참고**: `exceptionalCategories` 는 카테고리마다 다르다.
  `KC_CERTIFICATION` 이 포함되어 있으면 해당 카테고리는 KC 인증 관련
  처리가 필요함을 뜻한다. `certificationInfos` 는 모든 카테고리에서
  동일하므로 이 파일에는 **반복 저장하지 않는다** (용량 절약).
- **커버리지와 불완전 카테고리(중요)**: 리프 카테고리 4,999건 중 **91건**은
  상세 조회(`GET /categories/{id}`)가 429 Too Many Requests 로 실패했다.
  이 카테고리들은 최상위 `incomplete` 키(`{ids, count, reason, impact}`)에
  명시된다. 이 ID 들은 `exceptionalCategories` 가 빈 리스트로 남아 있어
  `KC_CERTIFICATION` 포함 여부를 확정할 수 없다 — 즉 **KC 필요 여부 불명**이다.
  `category_meta.requires_kc()` 는 이 ID 들에 대해 `False` 를 반환하지 않고
  `None`(불명)을 알리며, 컴플라이언스 게이트는 불명을 **통과시키지 않는다**
  (fail-closed). 실제 KC 대상을 면제로 오판하는 허위 신고를 막기 위해서다.
  수집 스크립트를 다시 실행해 이 91건의 상세를 채우면 `incomplete` 목록은
  비워진다.

### `certification_types.json`

- **출처**: 동일 API 의 `certificationInfos` 필드.
- **범위**: 인증 타입 마스터 목록 (57종). 모든 카테고리에서 동일하므로
  1회만 저장한다.
- **구조**: API 가 반환한 배열 구조를 그대로 유지.

### `notice_types.json`

- **출처**: 네이버 커머스 API 공식 문서(apicenter.commerce.naver.com,
  `productInfoProvidedNoticeType` 필드 정의 — `create-product-product`
  엔드포인트의 정적 문서 페이지) 및 공식 기술지원 공간 GitHub
  Discussions(commerce-api-naver/commerce-api)의 공개 글.
- **수집 방법**: 공개 문서 조사만. **상품 등록/수정/삭제 등 커머스 API 쓰기
  호출은 전혀 하지 않음**(API 호출 로그 0건).
- **범위**: `productInfoProvidedNoticeType` enum 값 목록과 타입별 하위
  노드/필드 구조.
  - `verified`(**36종**, 2026-08-12 갱신): **정본 API**
    `GET /external/v1/products-for-provided-notice` 응답에서 직접 받은 값.
    각 항목은 `field_meta`(필드별 `fieldType`·`fieldMaxLength`·`fieldDescription`·
    `fieldAddDescription`)를 함께 보관한다.
    ~~35종 · 공식 문서 enum 정의에서 관측~~ — **superseded**: 문서 조사 기반
    목록을 정본 API 응답으로 교체했다. 두 타입 이름 정정(`FASHION_ITEM` ->
    `FASHION_ITEMS`, `MICRO_ELECTRONICS` -> `MICROELECTRONICS`)은 그대로 유효.
  - `unverified`(**4종**): `LODGMENT_RESERVATION`, `TRAVEL_PACKAGE`,
    `AIRLINE_TICKET`, `RENT_CAR`. 정본 API 응답 36종에서 **찾지 못했다**
    (범위: 위 엔드포인트 응답 1건) — *존재하지 않는다는 뜻이 아니다.*
    `RENTAL_HA` 는 정본에 있어 **verified 로 승격**됐다.

  ★ **필드 목록이 둘이라는 것에 주의**: `field_meta` 는 정본 그대로이고,
  `fields` 는 **우리 게이트가 하드 필수로 요구하는** 목록이라 정본보다 짧다
  (35개 적음 · 21타입 — 그중 33개는 `~에 한함` 조건부라 의도적으로 제외).
  **통계·표를 뽑을 땐 `field_meta` 를 써라.** `fields` 로 뽑으면 과소 집계된다.
- **총 개수**: 공식 `create-product-product` 문서의 정적 enum 정의에는
  40개 값이 노출됨. 본 파일은 35/40 verified, 5/40 unverified.
  참고: 공식 maintainer 응답(#3490)에서는 GET
  `/v1/products-for-provided-notice` 기준 36종이라고 했으며, 40과 36의
  차이는 병합/제외 처리 또는 버전 차이로 추정됨.
- **제약**: 토큰·계정 식별자·스토어명·한자 0건. 한글 라벨은 허용된 리터럴.
- **갱신**: unverified 5종의 필드 구조는 동일 문서에서 별도 조사하면
  채울 수 있음.

### `notice_field_labels.json`

- **출처**: `notice_types.json` 의 필드명과 네이버 커머스 API 공식 문서의
  한국어 표시 문구.
- **목적**: 고시 필드의 camelCase 영어 이름을 사용자에게 보여줄 한국어 라벨과
  힌트 문구로 연결. 라벨은 **신고값이 아니라 표시 문구**다.
- **구조**:
  ```json
  {
    "generated_at": "<ISO8601 UTC>",
    "source": "https://apicenter.commerce.naver.com/docs/.../create-product-product",
    "note": "라벨은 사용자에게 무엇을 입력해야 하는지 알려주는 표시 문구다. 신고값이 아니다.",
    "labels": {
      "<fieldName>": { "label": "<한국어 이름>", "hint": "<왜 필요한지 한 줄>" }
    }
  }
  ```
- **범위**: 고시 필드 123종 중 라벨이 확정된 일부만 포함. 전 카테고리 공통
  5필드(`returnCostReason`, `noRefundReason`, `qualityAssuranceStandard`,
  `compensationProcedure`, `troubleShootingContents`)와 소재/치수/색상 등
  카테고리별 라벨. 라벨이 없는 필드는 영어 필드명 그대로 폴백한다.
- **제약**: 라벨이 확인되지 않은 필드는 **추측해 채우지 않는다**. 공식 문서에서
  별도 조사해 확보한 라벨만 추가한다.
- **조회**: `src/clossify/mcp_server.py` 의 `_notice_field_label(field)` 가
  이 파일을 1회 로드(캐싱)하여 `(라벨, 힌트)` 튜플을 반환한다. 파일이
  없거나 깨지면 stderr 에 사실을 알리고 필드명 폴백으로 동작한다.

## 생성 방법

수동 재실행으로 갱신한다 (자동 갱신 아님).

```sh
python scripts/fetch_category_meta.py
```

스크립트 특성:
- `clossify.naver_client` 의 인증(`get_token`/`_h`)을 재사용. 자체
  인증 구현 없음.
- **읽기 전용(GET) API 만 호출**한다. 상품 등록/수정/삭제 등 쓰기 호출은
  하지 않는다.
- 동시성 4, 요청 간 최소 간격, 429/5xx 지수백오프(최대 3회)로
  레이트리밋을 배려한다.
- **재개 가능**: 중간에 끊겨도 `.category_meta.progress.json` 진행상황
  파일에서 이어받는다 (전량 재요청 금지).
- 실패한 카테고리는 조용히 누락시키지 않고 `.category_meta.failed.json`
  에 남겨 보고한다.

## 갱신 주기

- 네이버 카테고리 분류는 연중 수시로 변경될 수 있다.
- 정기 갱신 주기는 없다. 의심되면 스크립트를 다시 실행하면 된다
  (재개 가능하므로 증분만 추가 조회).
- 진행상황/실패 파일(`.category_meta.progress.json`,
  `.category_meta.failed.json`)은 갱신을 위한 임시 파일이며 wheel 에
  포함되지 않는다.

## 조회 헬퍼

`src/clossify/category_meta.py` 가 이 데이터를 로드하여 조회 API 를
제공한다:

- `load_category_meta()` — 전체 메타 dict 로드
- `requires_kc(category_id)` — KC 인증 필요 여부. 3-상태:
  `True`(필요)/`False`(불필요, 확정)/`None`(불명 — `incomplete.ids` 에 포함된
  카테고리). 불명은 컴플라이언스 게이트에서 FAIL 로 차단된다.
- `exceptional_flags(category_id)` — 예외 플래그 목록
- `category_path(category_id)` — 전체 경로 문자열

데이터 파일이 부재하면 명확한 에러(`CategoryMetaUnavailableError`)를
발생시킨다 (조용한 빈 결과 금지).
