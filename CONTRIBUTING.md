# 기여 가이드 (Contributing)

이 프로젝트는 외부 기여를 환영합니다. 아래 절차를 따라 주세요.

## 기여 절차

1. **이슈 먼저** — 새로운 기능·버그 수정은 반드시 먼저 이슈로 올린다. 합의 없는 PR 은
   거부될 수 있다.
2. **브랜치** — `main` 에서 분기해 작업한다.
3. **PR** — 작업이 끝나면 `main` 으로 PR 을 연다. PR 설명에 관련 이슈 번호를 적는다.

## 커밋 전 검증 (필수)

PR 을 올리기 전에 **반드시** 아래 스크립트를 실행한다. 이 스크립트는 CI 가 실행하는
검사(`ruff check`, `ruff format --check`, `pip install -e ".[dev]"`, `pytest -q`,
`python scripts/scan_repo.py`)를 같은 순서로 실행한다. 하나라도 실패하면 exit 1 이며,
이 스크립트가 exit 0 이어야 CI 도 녹색이다.

```sh
pip install -e ".[dev]"            # 개발 의존성 (ruff 포함)
python scripts/verify_local.py     # 커밋 전 이것만 돌린다
```

> **ruff 버전 주의**: CI 는 `ruff==0.6.9` 로 고정되어 있다. 로컬에서 상위 버전을
> 쓰면 셀렉터 해석 차이로 "로컬은 통과, CI 는 실패"가 발생한다. 위 설치 명령으로
> `pyproject.toml` 의 `[project.optional-dependencies] dev` 에 고정된 버전이 설치된다.
> 자세한 안내는 `README.md` 의 "개발자용" 섹션을 본다.

## 코드 스타일

- **ruff** 설정(`pyproject.toml` 의 `[tool.ruff]`)을 따른다. 별도의 스타일 문서를
  두지 않고 ruff 설정이 단일 진실 원천이다.
- `ruff check` 와 `ruff format --check` 가 모두 통과해야 한다.
- 린트 위반은 `ruff check --fix` · `ruff format` 으로 자동 교정 가능한 경우가 많다.

## CLA (Contributor License Agreement)

외부 기여(코드·문서·데이터)를 병합하려면 **본 저장소의 CLA 에 동의**해야 한다.
최초 PR 에서 CLA.md 의 내용을 읽고 동의한다는 뜻으로 PR 설명에 CLA 를 언급하는
문구(예: "I have read and agree to the CLA")를 남긴다. 이 동의는 한 번이면 충분하다.

CLA 요지:

- 기여자는 저작권 사용 허락과 특허 라이선스를 프로젝트에 부여한다.
- 프로젝트 저작권자는 향후 재라이선스(듀얼 라이선스 포함) 권리를 보전한다.

전문은 `CLA.md` 를 본다.

## 라이선스

이 프로젝트는 **소스 공개(source-available)** fair-code 라이선스로 배포된다
(Sustainable Use License). "오픈소스"가 아님에 주의 — OSI 정의의 오픈소스가 아니다.
자세한 내용은 `LICENSE.md` 를 본다. 기여는 이 라이선스 조건 아래에서 이뤄진다.
