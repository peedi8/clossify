# 보안 정책 (Security Policy)

## 취약점 제보

이 저장소에서 보안 취약점을 발견했다면, **공개 이슈로 올리지 말고**
비공개 채널로 제보해 주세요.

- GitHub Security Advisory 의 "Report a vulnerability" 사용 (권장)
- 또는 `3rdhand.global@gmail.com` 으로 직접 이메일

제보 내용은 72시간 이내에 확인하고, 영향도 평가 후 패치 일정을 안내합니다.
책임있는 공개(responsible disclosure) 를 존중합니다 — 패치 전 공개는 자제해 주세요.

## 사용자 자격증명 구조

이 프로젝트는 **네이버 커머스 API / 검색광고 API 자격증명을 사용자 로컬 PC 의
설정 파일에만 저장**하는 구조입니다.

- 실제 자격증명은 `.local/config.json` 에 저장됩니다.
- `.local/` 디렉터리는 `.gitignore` 로 커밋에서 제외됩니다.
- `config.example.json` 은 **placeholder 값만** 포함하며, 실제 키를 넣으면 안 됩니다.

서버는 존재하지 않습니다. 모든 API 호출은 사용자 PC 에서 네이버 API 로 직행합니다.

## 키 유출 시 대응

**키가 유출된 것 같으면 즉시 행동하세요:**

1. **네이버 커머스API센터**에서 client_id / client_secret **재발급**
   - 기존 키는 즉시 폐기 (재사용 금지)
2. **네이버 검색광고**에서 API 키 / 시크릿키 / customer_id 재발급
3. 유출된 커밋이력이 있으면 `git filter-repo` 또는 BFG Repo-Cleaner 로 정리
4. GitHub Support 에 연락해 강제 push 권한 요청 (공개 저장소의 경우)

재발급 없이 키를 그대로 두면 스토어 탈취, 과금 남용, 계정 정지로 이어질 수 있습니다.

## 사전 방어선

이 저장소는 키 유출을 막기 위해 여러 방어선을 둡니다:

| 단계 | 도구 | 범위 |
|------|------|------|
| 커밋 직전 | `.pre-commit-config.yaml` 의 gitleaks | staged 변경분만 |
| PR/push 시 | `.github/workflows/ci.yml` 의 gitleaks | 전체 트리 + 히스토리 |
| PR/push 시 | `scripts/scan_repo.py` (CI) | 금칙어/한자/커밋메시지 |

이 방어선들은 GitHub 의 기본 secret scanning 이 네이버 계열 키 패턴을
잡지 못하는 빈틈을 메웁니다.

## 지원 범위

최신 릴리스 브랜치에 대해서만 보안 패치를 제공합니다.
구버전 사용자는 최신으로 업그레이드해 주세요.
