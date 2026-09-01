# 퀵스타트 — 설치 → 클라이언트 연결 → 첫 등록

> **상태**: 본 안내는 개발/기여자용이다. 패키지는 아직 pre-release 이며, 상태
> 표(`README.md` 의 *현재 상태*)에서 **진행 중**으로 표기된 항목은 이 흐름에서도
> 완료되지 않는다. 이 문서는 되지 않는 것을 된다고 쓰지 않는다.

이 문서는 **설치 직후 사용자가 멈추는 지점**을 메운다: 패키지는 설치됐지만 MCP
클라이언트에 이 서버를 **어떻게 등록하는지** 가 어디에도 적혀 있지 않다. 아래는
그 등록 한 번을 넘어 `check_config` 가 도는 것까지를 한 흐름으로 잇는다. 첫 등록
그 자체의 말투는 `README.md` 의 *이렇게 말하세요* 절에서 다루므로 여기서 반복하지
않고 링크로 잇는다(마지막 단계).

## 1. 설치

```sh
pip install -e .
```

요구: Python(버전은 `pyproject.toml` 기준). MCP 지원 클라이언트.

## 2. 설정 파일

`.local/` 은 gitignore 대상이라 clone 직후에는 **존재하지 않는다**. 따라서 설정 파일
복사 전에 디렉터리를 먼저 만들어야 한다. POSIX 셸과 Windows PowerShell 양쪽 명령을
두었다 — 쓰는 환경에 맞는 한쪽만 실행한다.

**POSIX(sh/bash/zsh, Linux·macOS·Git Bash)**:

```sh
mkdir -p .local
cp config.example.json .local/config.json
# .local/config.json 을 실제 값으로 채운다
```

**Windows PowerShell**:

```powershell
New-Item -ItemType Directory -Force -Path .local | Out-Null
Copy-Item config.example.json .local\config.json
# .local\config.json 을 실제 값으로 채운다
```

`mkdir -p`(POSIX)와 `New-Item -Force`(PowerShell)은 모두 **이미 디렉터리가 있어도
에러가 아니라** 그대로 통과한다. 첫 clone 이후에도 안전하게 다시 실행할 수 있다.

`.local/` 은 현재 작업 디렉터리(cwd) 아래에 만든다. 서버는 cwd 를 기준으로 상태
디렉터리를 정하기 때문에, **3단계의 `cwd` 설정과 같은 디렉터리**에 `.local/` 이
있어야 한다. 정본 경로는 `<cwd>/.local/config.json` 이고, 환경변수 `CLOSSIFY_CONFIG`
로 다른 경로를 가리킬 수 있다 (`CLOSSIFY_STATE_DIR` 로 상태 디렉터리 전체를
재정의할 수도 있다). `check_config` 가 **반드시 채워야 한다**고 검사하는 키는 2 개다:

- `naver.client_id`
- `naver.client_secret`

`naver.store_url_slug` 는 **선택**이다 — 값이 없어도 `check_config` 는 `ok=true`
를 돌려준다(부재는 `optional_absent` 로만 드러난다). 인증 서명은
`client_id`+`client_secret` 만 쓰고 API 는 슬러그를 요구하지 않으므로, 슬러그
부재가 어떤 기능도 막지 않는다.

값은 자리표시자(`REPLACE_WITH_...`, `{STORE_SLUG}` 등)가 아니라 **실제 발급값**이어야
한다. 자리표시자가 남아 있으면 `check_config` 가 채워지지 않은 것으로 본다. 원산지·
AS 전화번호·KC 신고값 등 고시 관련 실값도 등록 단계의 컴플라이언스 검사에서 필수다
(`README.md` 의 *필수 실값* 참고).

### 이 값들은 어디서 가져오나

발급처는 **네이버 커머스API 센터**(`apicenter.commerce.naver.com`)다. 검색·지도용인
**"네이버 개발자센터"가 아니다** — 이름이 같은 Client ID/Secret 이지만 전혀 다른
센터에서 받아야 한다.

- `client_id` / `client_secret`: 커머스API 센터의 메뉴
  **[애플리케이션] > [내스토어 애플리케이션]** 에서 애플리케이션을 등록하면 발급되는
  **애플리케이션 ID**와 **시크릿**이다.
- `store_url_slug`: 발급받는 값이 **아니다**. 본인 스토어 주소
  `https://smartstore.naver.com/<이_부분>` 의 그 부분이다.

사전 조건과 함정(발급 단계에서 챙기지 않으면 이후 원인이 안 보이는 것들):

- **통합매니저 권한**: 키 발급·조회에는 해당 스토어의 **통합매니저 권한**이 필요하다.
  권한이 없으면 커머스API 센터에서 애플리케이션을 만들거나 조회할 수 없다.
- **API 호출 IP**: 애플리케이션을 만들 때 **API 호출 IP 입력** 항목이 있다. 이 IP 가
  아니면 키가 있어도 호출이 막히고 원인이 명확히 드러나지 않는 경우가 많으므로 발급
  단계에서 반드시 챙긴다.
- **스토어당 1개**: 스토어 하나당 만들 수 있는 애플리케이션은 **최대 1개**다. 이미
  만들어둔 것이 있으면 새로 만들지 말고 그 값을 쓴다.

> 링크의 하위 경로는 확인되지 않았으므로 도메인까지만 적고 메뉴명으로 안내한다.
> 사이트 구조가 바뀔 수 있으니 커머스API 센터 화면에서 메뉴를 직접 확인한다.

## 3. 클라이언트에 서버 등록

이 서버는 stdio MCP 서버다. 클라이언트는 자식 프로세스로 서버를 띄우고 stdio 로
통신한다. 두 기동 경로 모두 실제 stdio 프로토콜로 검증됐고, 둘 다 같은 도구 열한
개를 노출한다:

| 경로 | 커맨드 | 인자 |
|---|---|---|
| 콘솔 스크립트 | `<venv>/Scripts/clossify.exe`(Windows) / `<venv>/bin/clossify`(POSIX) | 없음 |
| 모듈 실행 | `<venv>/Scripts/python.exe`(Windows) / `<venv>/bin/python`(POSIX) | `-m clossify.mcp_server` |

콘솔 스크립트 이름은 `pyproject.toml` 의 `[project.scripts]` 진입점(`clossify`)에서
온다. 클라이언트 설정 JSON 의 **형태**(어떤 키를 쓰는가)는 클라이언트 제품마다 다를
수 있다 — 아래는 **범용 stdio 등록 형태**이며, 실제 설정 파일 위치와 키 이름은 각
클라이언트의 문서를 따르라고만 여기서 정한다.

**Windows 예시**:

```json
{
  "command": "C:/path/to/.venv/Scripts/clossify.exe",
  "args": [],
  "cwd": "C:/path/to/project-root"
}
```

**POSIX 예시(모듈 실행 경로)**:

```json
{
  "command": "/path/to/.venv/bin/python",
  "args": ["-m", "clossify.mcp_server"],
  "cwd": "/path/to/project-root"
}
```

### `cwd` 를 반드시 넣는 이유

이것이 이 절에서 가장 자주 걸리는 함정이다. 이미지 업로드는 **작업 디렉터리(cwd)
하위 경로만** 허용한다 — 작업 디렉터리 밖의 사진 경로는 거부된다. 클라이언트가 서버
등록 시 `cwd` 를 주지 않으면, 서버 프로세스의 작업 디렉터리는 클라이언트가 정하는
어떤 기본값이 되고, 사용자가 건네는 사진 경로는 거의 항상 그 밖이 되어 **업로드가
막히고 사유가 분명하지 않게 된다.**

왜 이렇게 되는가: 서버가 임의 경로의 파일을 읽어 외부 이미지서버로 올리는 것을
막기 위해, 허용 루트를 작업 디렉터리(또는 아래의 `CLOSSIFY_UPLOAD_ROOT`)로 한정한다.
이것은 의도된 샌드박스 경계다 — 완화하려면 경로를 허용 루트 아래로 옮기거나
`CLOSSIFY_UPLOAD_ROOT` 로 루트를 명시적으로 지정하면 된다. 단, 루트를 넓힐수록 서버가
읽을 수 있는 파일의 범위도 넓어지므로 신중해야 한다.

요약: 클라이언트 등록 설정에 **반드시 `cwd` 를 프로젝트 루트로 지정한다.** 그래야
상대 사진 경로가 허용 루트 안에서 해석된다.

## 4. 연결 확인 — 이 단계가 갈림길이다

> **"클로시파이 설정 제대로 됐는지 확인해줘"**

이 말은 `check_config` 도구로 간다. **연결이 성공했는지 실패했는지를 가르는 지점이
바로 여기다.** 클라이언트가 이 도구를 찾지 못하거나 호출이 돌아오지 않으면 등록 설정이
잘못된 것이다 — 3단계로 돌아가 `command`/`args`/`cwd` 를 점검한다. 도구가 돌아오면
설정 파일의 필수 키·플레이스홀더·원산지 상태를 보고한다.

`check_config` 는 **값 자체를 출력하지 않는다** — 존재 여부만 알려 준다. 이것은
가시성 도구가 아니라 게이트다. 값이 새어나가는 일을 막기 위한 의도된 동작이다.

연결이 잡혔다면 클라이언트가 열 도구를 인식해야 한다: `check_config`,
`upload_images`, `pick_images`, `intake_detail_html`, `register_product`, `get_product`, `delete_product`,
`prepare_listing`, `submit_reviews`, `manage_products`, `get_category_attributes`,
`get_category_attribute_values`, `suggest_product_attributes`. 이 이름들이 보이지 않으면 서버가 뜌 경로가
잘못된 것이다.

## 5. 첫 등록으로 넘어가기

연결이 확인됐으면 첫 등록 흐름으로 넘어간다. 말투·되묻는 항목·미리보기 승인 게이트
등은 `README.md` 의 ***이렇게 말하세요*** 절에 정리해 두었다 — 여기서 반복하지 않는다.
핵심만 여기에: 사진 경로(2단계에서 정한 `cwd` 기준 상대경로, 또는 네이버 CDN URL)와
상품명·가격을 주면 된다.

## 막히는 지점 (실측된 것만)

- **IP 접근 제한**: 네이버 커머스 API 는 발급 시 등록한 IP 에서만 호출된다. 인증
  단계에서 막히면 가장 먼저 의심할 것. 본 도구가 바꿔 줄 수 있는 것이 아니다.
- **업로드 경로 제한**: 작업 디렉터리 밖의 사진 경로는 거부된다(위 *`cwd` 를 반드시
  넣는 이유* 참고). `cwd` 설정 또는 `CLOSSIFY_UPLOAD_ROOT` 로 해결한다.
- **미리보기 승인 게이트**: 기본이 켜져 있어 승인 없이는 등록이 거부된다. 이것은
  결함이 아니라 의도된 게이트다 — 판매자가 미리보기 HTML 을 눈으로 확인했다고
  선언해야 등록이 진행된다. 대량 등록 판매자는 설정에서 끌 수 있다.
- **자격증명 확인**: `check_config` 는 값을 출력하지 않는다(존재 여부만).

## 검증

이 문서의 사실(진입점 이름, 필수 키, 환경변수 이름, 도구 이름)은 `src/` 소스에서
읽어 비교하는 테스트가 지키고 있다 — `tests/test_quickstart_doc.py`. 코드가 움직이면
문서 테스트가 실패한다.
