---
name: design-system
description: {STORE_NAME} 상세페이지 디자인 시스템. 전체 룩=Airbnb(순백 캔버스·소프트 라운드·사진중심·절제된 타이포·넉넉한 여백·그림자 1단·warm 액센트 1개), 한국어 폰트 운용=KRDS/Toss/Gmarket 기준(Pretendard·lh 1.55·자간 0). 이 문서는 "룩(어떻게 보이나)"만 규정한다. 콘텐츠·옵션·이미지 처리 규칙은 [[COMPLIANCE_RULES]]가 source-of-truth이며 이 문서가 그것을 덮어쓰지 않는다.
---

## 0. 원칙 (디자인은 규칙 위에 얹는다)
- **이 문서 = 룩(스타일) 레이어.** [[COMPLIANCE_RULES]] = 콘텐츠/파이프라인 레이어(옵션 번호매칭·충실번역·3단폴백·실데이터·중국어0·금지어·단일컬럼 등). **룩이 규칙을 어기면 안 된다.**
- 특히 **옵션**: 디자인은 카드 모양만 바꾸고, 데이터는 반드시 파이프라인의 실제 옵션(optionName1, 번호배지=실제 번호, 충실번역+코드, 썸네일/크롭/카드 3단)을 쓴다. **임의 옵션 생성·이미지 재사용 금지.**

## 1. 색 (Airbnb base + warm 액센트)
```
canvas:      #ffffff   캔버스(순백). 다크모드 없음.
ink:         #222222   헤드라인·본문 주색 (순검정 금지)
body:        #3f3f3f   장문 본문 보조
muted:       #6a6a6a   서브라벨·캡션
hairline:    #ebebeb   디바이더 1px (강한 건 #dddddd)
surface-soft:#f7f7f7   옅은 필(썸네일 배경)
accent:      #a6634a   ★단 하나의 액센트(warm terracotta). eyebrow·DETAIL 라벨·번호배지에만 절제. 남발 금지(페이지의 90%는 흰+ink)
```
- 액센트는 Airbnb의 Rausch처럼 **희소하게** — 강조 한두 군데만.

## 2. 타이포 (한국어 = Pretendard, KRDS/Toss 기준)
```
font: "Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif
```
| 토큰 | size | weight | line-height | letter-spacing | 용도 |
|---|---|---|---|---|---|
| h1(상품명) | 28–30px | 600 | 1.35 | -0.01em | 상품 타이틀 |
| section | 22px | 600 | 1.4 | -0.01em | 섹션 헤딩(옵션 선택·상품 정보) |
| detail-h | 20–22px | 600 | 1.4 | -0.01em | DETAIL 소제목 |
| body | 17–18px | 400 | **1.55** | 0 | 본문(한글 가독성 표준) |
| caption | 14–15px | 500 | 1.45 | 0 | 캡션·정보라벨 |
| eyebrow | 12px | 700 | 1.3 | 0.1em(uppercase) | OVERSEAS DESIGN SELECT·DETAIL 라벨 |
| badge | 14px | 600 | 1 | 0 | 옵션 번호배지 |
| price | — | 800 | — | -0.02em | 가격(tabular-nums) |
- **한글 본문 line-height 1.5–1.55 필수**(KRDS). 자간 0(Pretendard가 이미 타이트). 헤딩만 -0.01~-0.02em.
- **weight 절제**: 헤딩도 600~700까지만. 사진이 시각 무게를 지므로 타이포는 과하지 않게(Airbnb 원칙).

## 3. 형태·간격·그림자 (Airbnb)
```
radius: card 16–18px / pill 9999px / sm 8px  (하드코너 금지, 사진·카드 전부 소프트 라운드)
spacing: base 16 / lg 24 / xl 32 / section 56–64px (섹션 간 넉넉히)
shadow(1단만): 0 0 0 1px rgba(0,0,0,.02), 0 2px 8px rgba(0,0,0,.05), 0 6px 16px rgba(0,0,0,.05)
```
- 깊이는 그림자 누적이 아니라 **사진 + 흰여백 + 라운드 클리핑**으로(Airbnb). 카드 hover/at-rest에 1단만.

## 4. 컴포넌트
- **상단/하단 배너**: `assets/brand/detail_header.png` / `detail_footer.png` 고정(불변, [[COMPLIANCE_RULES]] §6). 디자인시스템은 그 사이 **중간 컨텐츠**만 관할.
- **photo-block**: 본문 사진 1장=풀폭, radius 18px, shadow 1단. **단일컬럼**(§2). 원본 합성이미지는 통째 1장으로.
- **detail-section**: `DETAIL 0N`(eyebrow, accent) → 소제목(section) → 본문(body). 사진과 교차, section 간 56–64px.
- **option-card**: radius 16px, surface 흰, shadow 1단. 상단 썸네일(1:1, object-fit cover) + 좌상단 **번호배지**(rounded-full, accent 또는 ink 92%, 흰글자) + 하단 옵션명(`1. 샴페인색 MDYZ49` 충실). **데이터는 파이프라인 실옵션**(§5 준수). 이미지 없으면 설명+톤 카드(3단폴백).
- **info-row**: 라벨(muted, 120px) + 값(ink), 행마다 hairline 디바이더. 정의형 리스트.

## 5. Do / Don't
- **Do**: 흰 캔버스 90% + 사진이 주인공 + 액센트 한두 곳 + 한글 lh 1.55 + 소프트 라운드.
- **Don't**: 감성 미사여구(§4 카피규칙), 액센트 남발, 하드코너, 그림자 떡칠, 진한 색배경, **옵션 임의생성/이미지재사용**(규칙위반), 본문 멀티컬럼.

## 6. 적용
- 렌더 템플릿(중간 컨텐츠)을 이 토큰으로 작성 → 파이프라인 실데이터(상품명·사진·옵션·정보) 주입 → V19 고화질 렌더(2x·LANCZOS) → [고정헤더]+[이 중간]+[고정푸터].
- 추후 Figma `figma-generate-library`로 이 토큰을 Primitive/Semantic/Component 변수화 가능(동기화).
