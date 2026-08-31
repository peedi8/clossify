---
name: qa-agents
description: 등록 전 검수를 3개 전문 에이전트로 분할. 각자 [[COMPLIANCE_RULES]]의 담당 영역을 강제(게이트). 셋 다 PASS/WARN라야 register; 하나라도 FAIL이면 차단. vision_qa_agent.md(단일)를 이 3분할이 대체.
---

## 게이트 구조
`prepare_listing`(렌더) → **3검수 병렬** → 결과 합산: 전부 PASS/WARN→`register_product` / 하나라도 FAIL→차단+사유 회신(에이전트 탭 기록). 결정론 가능한 건 코드, 시각/언어 판단은 비전·텍스트 모델.

## MCP 도구 연동 (위임 신뢰 모델 — 타협 불가)
검수 회신은 `submit_reviews(product_key, reviews)` 도구로 서버에 전달된다. 서버가 이를
prepared payload 의 QA 기록에 **병합**할 때 다음 규약이 적용된다(`src/clossify/mcp_server.py`
및 `src/clossify/register.py` 의 실제 동작):

- **제출 가능 agent 는 `image`·`copy` 두 개뿐.** `compliance` verdict 를 `submit_reviews`
  로 보내면 서버가 거부한다(`ValueError`). 컴플라이언스(결정론 검사)는 클라이언트가
  뒤집을 수 없다.
- **병합은 최악값을 채택**한다(FAIL > PENDING > WARN > PASS). 서버가 스스로 기록한
  violations 은 절대 삭제되지 않는다.
- 따라서 클라이언트 회신은 `PENDING → PASS` 상향만 가능하고, `FAIL → PASS` 는 불가능하다.
- 병합 후 `submit_reviews` 는 `gate_allowed` 를 반환해 등록 가능 여부를 알린다.
- 최종 등록 게이트는 `register_product` 안에서 다시 실행된다(preview_confirmed 선언 +
  컴플라이언스 결정론 + prepared QA 집계).

## 1. 이미지 검수 (qa_image) — 비전 + 코드
- **레이아웃**(코드): 본문 detail.html에 grid/다열 마크업 0 ([[COMPLIANCE_RULES]] §2, 원본합성 1장은 OK).
- **외국어 텍스트 0**(비전): 이미지 어디든 외국어 라벨 잔존 시 FAIL (판매자 식별 숫자는 허용).
- **과도 흰여백**(코드+비전): 상/하 균일 흰여백 12%↑면 FAIL→자동 트림 라우팅([[COMPLIANCE_RULES]] §3).
- **품질**: 깨짐·심한왜곡·비율뭉개짐·빈 옵션카드 과다 → FAIL.
- **옵션이미지**: 옵션 빠짐 없이 전부 처리됐나(썸네일/크롭/카드 3단).

## 2. 카피/글 검수 (qa_copy) — 텍스트 + 비전 보조
- **제목**([[COMPLIANCE_RULES]] §7): 네이버 공식 어순·앞가중치, 금지어(정품·최고·1위) 0, 지명/왕조/고유명사·직역체 과다면 WARN(네이밍 회송).
- **본문 카피**([[COMPLIANCE_RULES]] §4·§13·[[COPY_GUIDE]]): 기능·활용+혜택(스펙→혜택), **감성 미사여구·가짜통계(재구매율 등) 금지**, 오프닝 훅 자연스러움.
- **옵션명 충실**: 옵션명=사용자 입력 한국어 명칭+코드유지([[COMPLIANCE_RULES]] §5), 드롭다운=라벨 일치.
- **금지어**: 정품/AUTHENTIC/100%/최고/공식 어디에도 0. 단 **공식** 은 `가공식품`·`축산가공식품` 등 정상 복합명사(뒤에 `품` 이 오는 경우) 와 `비공식`(부정 접두사) 은 예외([[COMPLIANCE_RULES]] §1 비고).

## 3. 컴플라이언스 검수 (qa_compliance) — 코드 + 구조 (클라이언트 제출 불가)
이 축은 결정론 코드 검사(`src/clossify/qa_agents.py`·`mcp_server._run_compliance_gate`)가
주체다. 클라이언트 LLM 은 `submit_reviews` 의 `agent="compliance"` 회신으로 이 결과를
뒤집을 수 없다. 아래 항목은 서버가 `register_product` 직전에 자체 검사한다.
- **원산지**([[COMPLIANCE_RULES]] §12): 사용자가 제공한 구체적 국가명 명시, "해외"·"기타" 단독 0.
- **교환·반품 5요소**([[COMPLIANCE_RULES]] §12): 고정 푸터/상세에 청약철회기간·반품비부담·불가사유·환불기간·판매자연락처 존재.
- **고시/속성**([[COMPLIANCE_RULES]] §7·§12): **고시 타입마다 필수 필드가 다르다**(`data/notice_types.json` 이 정본). 빈칸/더미 0.
- **KC**: `category_meta.requires_kc` 기반 — 필요 카테고리는 인증표기, 불필요는 미표기.
- **옵션 무결**: 옵션 안 빠짐(전 옵션 등록), 번호매칭(1.↔①) 일관, 품절 반영.

## 출력 (각 에이전트)
`{agent:"image|copy|compliance", verdict:"PASS|WARN|FAIL", violations:[{rule,severity,detail}]}` → `submit_reviews` 병합(최악값 채택) + 에이전트 탭 기록(상품별 3행).
`image`·`copy` 회신만 `submit_reviews` 가 수용하고, `compliance` 회신은 서버가 거부한다.

## qa_copy SEO 제목 추가 기준
- SEO 상품명은 키워드 덤프가 아니라 읽히는 구문이어야 한다.
- 공백 기준 6~9유닛을 목표로 하며, 9유닛 초과는 FAIL이다.
- 핵심 제품명/제품유형은 제목 앞 3유닛 안에 있어야 한다.
- 색상은 셀링포인트가 명확할 때 1개까지만 허용한다. 화이트/그레이처럼 색상만 여러 개 붙이면 FAIL이다.
- 인테리어/거실인테리어/사무실인테리어/홈데코/소품샵/데코류는 1개만 남긴다.
- 테이블/탁상/거실 같은 배치맥락 명사는 핵심 제품 뒤에 1개까지만 허용한다.
- 비조명 상품의 조도/루멘/색온도, 비가전의 소비전력/용량, 비가구의 접이식테이블/의자는 타카테고리 용어로 FAIL이다.
