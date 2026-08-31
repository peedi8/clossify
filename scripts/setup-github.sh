#!/usr/bin/env bash
# GitHub 저장소 보호 설정 스크립트.
#
# 사용법:
#     bash scripts/setup-github.sh <owner>/<repo> [--solo]
#
# 수행 항목:
#   1. 브랜치 보호 (main) — required status checks = CI 잡 이름 4개
#   2. 머지 전략 (squash-merge 권장)
#   3. 라벨 적용 (security, qa, compliance 등)
#
# 옵션:
#   --solo   PR 승인 요구를 끈다. 1인 프로젝트에서 승인자가 없어
#            머지가 영구 차단되는 것을 방지.
#
# ⚠️ 이 스크립트는 실행하지 말 것 — 작성만 수행.
#    원격 설정 변경은 오케스트레이터·사용자 소관.
#    (원래 요구사항의 Hard Constraints 에 명시됨)
#
# 설계:
#   - 보호 적용 실패해도 나머지(머지 전략, 라벨)는 계속 적용한다.
#   - 마지막에 무엇이 빠졌는지 명확히 출력한다 (조용한 누락 금지).

set -uo pipefail

OWNER_REPO="${1:-}"
SOLO=0

# 인자 파싱.
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --solo) SOLO=1; shift ;;
        *) echo "[setup-github] 알 수 없는 인자: $1" >&2; shift ;;
    esac
done

if [[ -z "$OWNER_REPO" ]]; then
    echo "사용법: bash scripts/setup-github.sh <owner>/<repo> [--solo]" >&2
    exit 2
fi

# CI 잡 이름 — .github/workflows/ci.yml 의 job name 과 정확히 일치해야 함.
# 이 배열이 required status check 목록이 된다. 이름을 바꾸면 양쪽을 같이.
REQUIRED_CHECKS=(
    "gitleaks"
    "ruff"
    "pytest"
    "scan-repo"
)

# 라벨 정의 — 이름/색상/설명.
LABELS=(
    "security:B60209:보안·시크릿 관련"
    "qa:0E8A16:품질 보증 회귀"
    "compliance:1D76DB:컴플라이언스 규칙"
    "data:BFD4F2:카테고리/메타데이터"
    "infra:FBCA04:CI/인프라/빌드"
    "docs:0075CA:문서"
)

FAILED=()

echo "[setup-github] 대상: $OWNER_REPO  (solo=$SOLO)"

# ──────────────────────────────────────────────────────────────────────
# 1. 라벨 적용 — 실패해도 계속 진행.
# ──────────────────────────────────────────────────────────────────────
echo "[setup-github] 라벨 적용 중..."
for entry in "${LABELS[@]}"; do
    IFS=':' read -r name color desc <<<"$entry"
    if gh label create "$name" \
        --color "$color" \
        --description "$desc" \
        --repo "$OWNER_REPO" \
        --force >/dev/null 2>&1; then
        echo "  [ok] label: $name"
    else
        echo "  [SKIP] label 실패: $name" >&2
        FAILED+=("label:$name")
    fi
done

# ──────────────────────────────────────────────────────────────────────
# 2. 머지 전략 — squash-merge 만 허용, 나머지는 끔.
# ──────────────────────────────────────────────────────────────────────
echo "[setup-github] 머지 전략 설정 중..."
if gh repo edit "$OWNER_REPO" \
    --enable-merge=false \
    --enable-squash-merge \
    --enable-rebase-merge=false \
    --delete-branch-on-merge \
    >/dev/null 2>&1; then
    echo "  [ok] squash-merge only, 브랜치 자동 삭제"
else
    echo "  [SKIP] 머지 전략 설정 실패" >&2
    FAILED+=("merge-strategy")
fi

# ──────────────────────────────────────────────────────────────────────
# 3. 브랜치 보호 — required status checks = CI 잡 이름 4개.
# ──────────────────────────────────────────────────────────────────────
echo "[setup-github] 브랜치 보호 설정 중..."

# required checks 문자열 배열을 comma-separated 로 변환.
checks_csv=$(IFS=,; echo "${REQUIRED_CHECKS[*]}")

PROTECTION_BODY=$(cat <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": $(printf '%s\n' "${REQUIRED_CHECKS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
)

# --solo 가 아니면 PR 리뷰 1건 요구.
if [[ "$SOLO" -eq 0 ]]; then
    PROTECTION_BODY=$(cat <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": $(printf '%s\n' "${REQUIRED_CHECKS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null
}
EOF
)
fi

# REST API 직접 호출 (gh cli 의 protection 서브커맨드 호환성 이슈 회피).
if gh api -X PUT \
    "repos/${OWNER_REPO}/branches/main/protection" \
    --input - <<<"$PROTECTION_BODY" >/dev/null 2>&1; then
    echo "  [ok] required checks: ${checks_csv}"
    [[ "$SOLO" -eq 0 ]] && echo "  [ok] PR review: 1 (solo off)" \
                       || echo "  [ok] PR review: disabled (--solo)"
else
    echo "  [SKIP] 브랜치 보호 설정 실패" >&2
    FAILED+=("branch-protection")
fi

# ──────────────────────────────────────────────────────────────────────
# 보고 — 무엇이 빠졌는지 명확히.
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "[setup-github] 완료."
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "[setup-github] ⚠️ 다음 항목이 누락되었음 — 수동 점검 필요:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    exit 0   # 부분 실패는 exit 0 — 나머지는 적용됐으므로.
fi

echo "[setup-github] 전체 적용 성공."
