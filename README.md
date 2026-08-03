# Clossify

네이버 스마트스토어 상품 자동등록 **로컬 MCP 서버**. 사용자 사진과 최소 정보에서
출발해 상세페이지 생성 · 상품명/SEO · 상품정보제공고시 작성 · 커머스 API 등록까지
한 흐름으로 잇는다. 서버 호스트는 없고, 자격증명은 사용자 PC의 설정 파일에만 있다.

## 현재 상태

**개발 중(pre-release).** 아직 사용 가능한 릴리스가 아니다. 되는 것·진행 중인 것·
없는 것을 아래 표에 정확히 구분했다 — 과대 표기 없음.

| 구분 | 항목 | 비고 |
|------|------|------|
| 되는 것 | MCP 도구 4개(`check_config`/`upload_images`/`register_product`/`get_product`) | 실제 커머스 API 등록 관통 |
| 되는 것 | 결정론 컴플라이언스 게이트(fail-closed) | 등록 직전 FAIL 위반 차단 |
| 되는 것 | 카테고리 메타 데이터(리프 카테고리 약 4,999건)·고시 타입 35종 데이터 기반 검사 | `data/` |
| 되는 것 | 이미지 입력 정규화(로컬 파일/외부 URL/이미 업로드된 CDN URL) | 매직바이트·확장자·크기·SSRF 가드 |
| 되는 것 | API 응답 sanitization(시크릿/경로/traceback 마스킹) | `mcp_server._sanitize_*` |
| 진행 중 | 상세페이지 렌더(`register._render_detail_html`) | `NotImplementedError` 스텁 |
| 진행 중 | prepare 파이프라인 본체(`register.register_listing`의 입력→prepared 경로) | 스텁 |
| 진행 중 | LLM 판단 위임 왕복(카피 QA·이미지 QA 회신 접합) | `pending_reviews`로 표기만, 미연결 |
| 아직 없음 | 이미지 생성 어댑터 연동 | config 키만 존재 |
| 아직 없음 | 멀티몰 | 네이버 단일 |
| 아직 없음 | GUI | stdio MCP 만 |

> 컴플라이언스 게이트는 현재 결정론 위반(FAIL)만 차단한다. LLM 판단이 필요한
> 카피/이미지 QA는 위임 왕복 연결 전이라 `pending_reviews`로 응답에 표기된다
> (조용한 생략 아님). 집계 게이트(`qa_agents.qa_gate`) 정책은 PENDING 도 차단이며,
> 도구 회선의 게이트와 집계 게이트의 차이는 `docs/ARCHITECTURE.md` 참고.

## 어떻게 쓰는가(개념)

MCP의 UI는 자연어다. 사용자가 "이 사진들로 등록해줘"라고 말하면, 클라이언트 LLM이
아래 도구를 호출한다. 서버는 검증하고 부족한 값이 있으면 무엇이 필요한지 되묻는다.

### 도구

| 도구 | 역할 | 외부 API 호출 |
|------|------|---------------|
| `check_config` | 설정 파일 존재·필수 키·플레이스홀더·원산지 설정 여부 검사(값 미노출) | 없음 |
| `upload_images` | 로컬 이미지 경로 리스트를 검증 후 네이버 이미지서버에 업로드 → CDN URL 반환 | 네이버 이미지서버(쓰기) |
| `register_product` | 상품 정보를 받아 페이로드 빌드 → 컴플라이언스 게이트 → 네이버 커머스 API 등록 | 네이버 커머스(쓰기) |
| `get_product` | 등록된 상품(origin product)을 조회(재검증용) | 네이버 커머스(읽기) |

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

> **ruff 버전 주의(T-205b)**: CI(`.github/workflows/ci.yml`)는 `ruff==0.6.9` 로
> 고정되어 있다. 로컬에서 상위 버전을 쓰면 셀렉터 해석 차이로 "로컬은 통과, CI 는
> 실패"가 발생한다(실제로 `ISC004`/`RUF059` 셀렉터가 상위 버전에만 있어 0.6.9 에서
> 설정 파싱이 exit 2 로 실패한 사례가 있었다). 반드시 CI 와 **동일한 버전**을 설치할
> 것. 아래 명령으로 개발 의존성을 설치하면 `pyproject.toml` 의
> `[project.optional-dependencies] dev` 에 `ruff==0.6.9` 가 고정되어 있다.

```sh
pip install -e ".[dev]"            # ruff==0.6.9 포함 개발 의존성 설치
ruff check .                       # 린트 — CI 와 동일 명령/구성
pytest                             # 기존 테스트
python scripts/scan_repo.py        # 금칙어·한자·커밋메시지 스캔, 위반 시 exit 1
```

pre-commit(gitleaks 등) 설정은 저장소 참고. 자세한 설계·모듈 의존·데이터 자산은
`docs/ARCHITECTURE.md`, 배경 결정은 `docs/adr/` 참고.

## 면책

- 본 도구는 판매자의 스토어에 상품을 등록한다. 등록 결과(정책 위반·표시 오류 등)의
  **최종 책임은 판매자에게 있다.**
- 본 도구는 등록 전 검증을 돕지만, **플랫폼 정책과 관련 법령 준수를 보증하지 않는다.**
- 원산지·인증·고시값 등 규제 신고값은 **판매자가 설정한 값 그대로** 전송된다.
  도구가 임의로 추정해 채우지 않는다.

## 라이선스

**미확정**(fair-code 계열 검토 중). 확정 전까지 모든 권리 보유. 외부 기여 수용 시
CLA 요구로 재라이선스 권리를 보전한다.

## 더 보기

- `docs/ARCHITECTURE.md` — 모듈 구조, 데이터 자산, 등록 흐름, QA verdict 체계
- `docs/adr/` — 아키텍처 결정 기록(0001~0004)
- `SECURITY.md` — 보안 정책, 키 유출 대응
- `data/README.md` — 카테고리/고시/인증 메타데이터 출처와 갱신
