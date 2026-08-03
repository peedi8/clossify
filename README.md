# Clossify

네이버 스마트스토어 상품 자동등록 **로컬 MCP 서버**. 사용자 사진과 최소 정보에서
출발해 상세페이지 생성 · 상품명/SEO · 상품정보제공고시 작성 · 커머스 API 등록까지
한 흐름으로 잇는다. 서버 호스트는 없고, 자격증명은 사용자 PC의 설정 파일에만 있다.

## 현재 상태

**개발 중(pre-release).** 아직 사용 가능한 릴리스가 아니다. 되는 것·진행 중인 것·
없는 것을 아래 표에 정확히 구분했다 — 과대 표기 없음.

| 구분 | 항목 | 비고 |
|------|------|------|
| 되는 것 | MCP 도구 6개(`check_config`/`upload_images`/`register_product`/`get_product`/`prepare_listing`/`submit_reviews`) | 실제 커머스 API 등록 관통 |
| 되는 것 | 결정론 컴플라이언스 게이트(fail-closed) | 등록 직전 FAIL 위반 차단 |
| 되는 것 | 카테고리 메타 데이터(리프 카테고리 약 4,999건)·고시 타입 35종 데이터 기반 검사 | `data/` |
| 되는 것 | 이미지 입력 정규화(로컬 파일/외부 URL/이미 업로드된 CDN URL) | 매직바이트·확장자·크기·SSRF 가드 |
| 되는 것 | API 응답 sanitization(시크릿/경로/traceback 마스킹) | `mcp_server._sanitize_*` |
| 되는 것 | 상세페이지 렌더 + 편집 가능 문서(`detail_render.render_detail_html`/`build_scene`) | 섹션 조립 결과를 HTML 과 구조 문서로 동시 산출 |
| 되는 것 | prepare 파이프라인 본체(`register.prepare_listing`) | 이미지 정규화 → 상세 렌더 → JPEG 비의존 QA → prepared payload 저장 |
| 되는 것 | LLM 판단 위임 왕복(`submit_reviews`) | 미회신은 PENDING 으로 게이트 차단, 회신 시 통과 |
| 아직 없음 | 이미지 생성 어댑터 연동 | config 키만 존재 |
| 아직 없음 | 멀티몰 | 네이버 단일 |
| 아직 없음 | GUI | stdio MCP 만 |

> **게이트 정책**: 결정론 검사는 등록 직전 FAIL 위반을 차단한다(fail-closed).
> LLM 판단이 필요한 카피/이미지 QA는 `prepare_listing` 경로에서 PENDING 으로
> 등록되고, 집계 게이트(`qa_agents.qa_gate`)는 PENDING 도 차단하므로 미회신이면
> 등록이 막힌다. 클라이언트는 `submit_reviews` 로 회신하며, 회신은 서버 판정과
> **최악값 병합**(FAIL > PENDING > WARN > PASS)이므로 서버 FAIL 을 뒤집을 수
> 없고, 결정론 검사(`compliance`)는 제출 자체가 거부된다. `register_product` 를
> 직접 부를 때 prepared 기록이 있으면 완전 게이트(PENDING/FAIL 차단), 없으면
> 결정론 검사만 적용하고 응답의 `gate` 필드로 그 사실을 표기한다. 도구 회선과
> 집계 게이트의 차이는 `docs/ARCHITECTURE.md` 참고.

## 어떻게 쓰는가(개념)

MCP의 UI는 자연어다. 사용자가 "이 사진들로 등록해줘"라고 말하면, 클라이언트 LLM이
아래 도구를 호출한다. 서버는 검증하고 부족한 값이 있으면 무엇이 필요한지 되묻는다.

**전형 흐름**: 사용자가 상품명·가격·사진(로컬 파일 또는 URL)을 건네면, 클라이언트는
`prepare_listing` 으로 이미지 정규화·상세페이지 조립·QA 집계를 한 번에 수행한다.
서버는 JPEG 비의존 QA는 바로 실행하고, 이미지/카피 품질처럼 LLM 판단이 필요한
항목은 PENDING 으로 돌려 needs_llm 에 담아 who-asks-what 을 알려준다. 클라이언트가
판단을 내려 `submit_reviews` 로 회신하면 PENDING 이 해소되고, 부족한 고시 값은
사용자에게 되물어 보완한다. 게이트를 통과하면 `register_product` 가 커머스 API 로
실제 등록을 하고 `get_product` 로 재검증한다. 요약하면 **사진을 주면 → 부족한 값을
되묻고 → 확인 후 등록** 된다. 상세페이지 조립 결과는 편집 가능한 구조 문서로도
같이 산출되며, 스펙은 `docs/scene-schema.md`.

### 도구

| 도구 | 역할 | 외부 API 호출 |
|------|------|---------------|
| `check_config` | 설정 파일 존재·필수 키·플레이스홀더·원산지 설정 여부 검사(값 미노출) | 없음 |
| `upload_images` | 로컬 이미지 경로 리스트를 검증 후 네이버 이미지서버에 업로드 → CDN URL 반환 | 네이버 이미지서버(쓰기) |
| `register_product` | 상품 정보를 받아 페이로드 빌드 → 컴플라이언스 게이트 → 네이버 커머스 API 등록 | 네이버 커머스(쓰기) |
| `get_product` | 등록된 상품(origin product)을 조회(재검증용) | 네이버 커머스(읽기) |
| `prepare_listing` | 상품 정보 + 이미지 소스로 등록 전 준비: 이미지 정규화, 상세페이지 렌더, JPEG 비의존 QA 집계 후 prepared payload 저장. LLM 판단이 필요한 항목(needs_llm)과 사용자 입력이 필요한 항목(needs_user)을 알려준다 | 없음(로컬 검증만) |
| `submit_reviews` | 클라이언트 LLM 의 카피/이미지 QA 판단을 prepared payload 의 QA 기록에 병합. 회신은 서버 판정과 최악값 병합(PENDING→PASS 만 허용, 서버 FAIL 불가)이며 compliance 제출은 거부된다 | 없음(prepared payload 갱신만) |

## 설계 원칙

1. **로컬 실행·BYO-key** — 자격증명이 사용자 PC를 벗어나지 않는다. 서버 호스트 없음.
2. **결정론 검증은 서버가 담당** — 클라이언트 LLM이 관대해도 서버 코드가 막는다
   (fail-closed).
3. **사실을 지어내지 않는다** — 소재·치수·인증번호처럼 사용자만 아는 값은 요구하고,
   추정해 채우지 않는다. 빈 값은 빈 값으로 보고한다.
4. **범용 카테고리** — 품목을 가정하지 않고 카테고리/고시 데이터로 판정한다.

## 설치·설정

> 준비 중인 pre-release. 아래는 개발/기여자용 안내다.

**요구**: Python(버전은 `pyproject.toml` 기준). MCP 지원 클라이언트.

```sh
pip install -e .
cp config.example.json .local/config.json
# .local/config.json 을 실제 값으로 채운다
```

### 필요한 자격증명

| 항목 | 발급처 | 비고 |
|------|--------|------|
| 네이버 커머스 API(`naver.client_id`/`client_secret`) | 커머스 API 센터(셀러 본인 발급) | OAuth2 client_credentials. `store_url_slug` 필수 |
| 검색광고 API(`naver_searchad.api_key`/`secret_key`/`customer_id`) | 네이버 검색광고 | SEO 키워드 볼륨 |

이미지 생성 API(선택)는 어댑터 연동 전이므로 현재 미사용.

### 필수 실값(경고)

아래 값은 **판매자가 실제 신고하는 진짜 값**이어야 한다. 안내문구/플레이스홀더를
넣으면 컴플라이언스 게이트가 등록을 거부한다(fail-closed, 조용한 기본값 없음).

- `smartstore_notice_defaults.origin_area_code` / `origin_content` — 원산지
- `smartstore_notice_defaults.manufacturer`/`importer`/`model_name` — 해당 시
- `smartstore_notice_defaults.as_tel` — AS 전화번호(실번호). 정본 위치. `brand.as_tel` 은 에이전트 문서 `{AS_TEL}` 치환용이며 비어있으면 정본을 참조
- `kc_declaration.kcCertifiedProductExclusionYn` / `kcExemptionType` — KC 인증 대상 카테고리

## 개발자용

> **ruff 버전 주의**: CI(`.github/workflows/ci.yml`)는 `ruff==0.6.9` 로
> 고정되어 있다. 로컬에서 상위 버전을 쓰면 셀렉터 해석 차이로 "로컬은 통과, CI 는
> 실패"가 발생한다(실제로 `ISC004`/`RUF059` 셀렉터가 상위 버전에만 있어 0.6.9 에서
> 설정 파싱이 exit 2 로 실패한 사례가 있었다). 반드시 CI 와 **동일한 버전**을 설치할
> 것. 아래 명령으로 개발 의존성을 설치하면 `pyproject.toml` 의
> `[project.optional-dependencies] dev` 에 `ruff==0.6.9` 가 고정되어 있다.

### 커밋 전 검증 — `scripts/verify_local.py` 한 번이면 충분

개별 린트/테스트 명령을 따로 돌리지 말고 **이 스크립트 하나**를 돌린다. CI 가
실행하는 검사(`ruff check`·`ruff format --check`·`pip install -e ".[dev]"`·
`pytest -q`·`python scripts/scan_repo.py`)를 **같은 순서로 그대로** 실행하므로,
이 스크립트가 exit 0 이면 CI 도 녹색이다. 하나라도 실패하면 exit 1 이며 어느
단계에서 깨졌는지 바로 보인다. ruff 버전이 CI 와 다르면 경고를 출력한다.

```sh
pip install -e ".[dev]"            # ruff==0.6.9 포함 개발 의존성 설치
python scripts/verify_local.py     # 커밋 전 이것만 돌리면 CI 와 같은 검사를 한다
```

> 이 스크립트와 `.github/workflows/ci.yml` 은 한 쌍이다. 워크플로를 바꾸면
> 스크립트도 함께 바꾼다(스크립트 상단 주석에 같은 안내가 있다).

pre-commit(gitleaks 등) 설정은 저장소 참고. 자세한 설계·모듈 의존·데이터 자산은
`docs/ARCHITECTURE.md`, 배경 결정은 `docs/adr/` 참고.

## 면책

- 본 도구는 판매자의 스토어에 상품을 등록한다. 등록 결과(정책 위반·표시 오류 등)의
  **최종 책임은 판매자에게 있다.**
- 본 도구는 등록 전 검증을 돕지만, **플랫폼 정책과 관련 법령 준수를 보증하지 않는다.**
- 원산지·인증·고시값 등 규제 신고값은 **판매자가 설정한 값 그대로** 전송된다.
  도구가 임의로 추정해 채우지 않는다.

## 라이선스

**Sustainable Use License(fair-code, 소스 공개)**. (1) 개인·자사 내부 사용·수정·열람은 무료,
(2) 무상·비상업 목적의 재배포 가능, (3) **상업 서비스로 제공하는 권리는 저작권자 보유**.
이 라이선스는 OSI 정의의 오픈소스가 **아니라** source-available 이다. 전문은 `LICENSE.md`.
요약은 안내일 뿐이며 **구속력은 원문에 있다**. 외부 기여 수용 시 `CLA.md` 로 재라이선스
권리를 보전한다.

## 더 보기

- `docs/ARCHITECTURE.md` — 모듈 구조, 데이터 자산, 등록 흐름, QA verdict 체계
- `docs/adr/` — 아키텍처 결정 기록(0001~0004)
- `SECURITY.md` — 보안 정책, 키 유출 대응
- `data/README.md` — 카테고리/고시/인증 메타데이터 출처와 갱신
