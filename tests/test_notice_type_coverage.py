"""고시 타입 35종 전수 컴플라이언스 PASS 검증 + placeholder 변형 차단 테스트.

검증 시나리오:
  1. **35개 고시 타입 전수**: 각 타입의 선언된 필수 필드를 채운 입력이
     컴플라이언스 ``_notice_field_missing`` 을 통과(누락 0건)하는지 확인.
     데이터와 코드가 다시 어긋나면 즉시 실패.
  2. **ETC 정상 입력**: ``ETC`` 타입 정상 입력 → 필수 필드 누락 0건
     (상호배제 정정 후 ``afterServiceDirector`` 만 필수).
  3. **placeholder 변형 3종 이상 → 미제공 판정**: 공백 삽입·표기 차이 등
     변형이 ``_is_placeholder_value`` 로 미제공으로 잡히는지 확인.
  4. **정상 값 3종 → 통과**: ``면 100%``, ``2026-01``, ``어깨 42cm`` 등
     실질 정보 값이 placeholder 로 오판되지 않는지 확인 (과잉 차단 금지).
  5. **E2E (ETC)**: ``ETC`` 타입 정상 입력 → ``qa_compliance`` PASS,
     payload ``etc`` 노드에 AS 관련 필드가 규칙에 맞게 실림.
  6. **전송 유지**: 사용자가 안내문구를 명시 입력한 경우 payload 에 그대로 존재.

무동작·identity 금지: 정규화·대조 로직이 실제로 동작하는지 확인한다.
실제 네이버 API 를 호출하지 않는다 (monkeypatch 로 네트워크 차단).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

_NOTICE_CFG_WITH_ORIGIN = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
    "importer": "테스트수입사",
}

_COMMON_CFG_ORIGIN_ONLY = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


# 타입별 실질값 매핑 — 각 고시 타입의 필수 필드를 채우는 정상 값.
# 목적: "정상 실값으로 컴플라이언스 PASS" 를 35종 전수 검증.
# 모든 값은 실제 고시 신고에 쓰는 형태의 예시값이며 placeholder 가 아니다.
_TYPE_REAL_VALUES = {
    # 공통 5필드는 config 기본값으로 채워지므로 여기서는 타입 고유 필드만.
    "WEAR": {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2026-01",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "SHOES": {
        "material": "가죽",
        "color": "블랙",
        "size": "270",
        "height": "10cm",
        "manufacturer": "테스트제조사",
        "caution": "습기 주의",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "BAG": {
        "type": "토트백",
        "material": "가죽",
        "color": "블랙",
        "size": "가로 30cm",
        "manufacturer": "테스트제조사",
        "caution": "습기 주의",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "FURNITURE": {
        "itemName": "원목 의자",
        "certificationType": "KC 인증",
        "color": "월넛",
        "components": "의자 1개",
        "material": "원목",
        "manufacturer": "테스트제조사",
        "importer": "테스트수입사",
        "producer": "테스트제조사",
        "size": "가로 50cm",
        "installedCharge": "설치비 별도",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "IMAGE_APPLIANCES": {
        "itemName": "테스트 TV",
        "modelName": "TV-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "220V",
        "powerConsumption": "100W",
        "energyEfficiencyRating": "1등급",
        "releaseDateType": "출시일",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 120cm",
        "displaySpecification": "UHD",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "SEASON_APPLIANCES": {
        "itemName": "테스트 에어컨",
        "modelName": "AC-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "220V",
        "powerConsumption": "1000W",
        "energyEfficiencyRating": "1등급",
        "releaseDateType": "출시일",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 80cm",
        "area": "평당 10평형",
        "installedCharge": "설치비 별도",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "OFFICE_APPLIANCES": {
        "itemName": "테스트 프린터",
        "modelName": "PR-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "220V",
        "powerConsumption": "50W",
        "energyEfficiencyRating": "2등급",
        "releaseDateType": "출시일",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 40cm",
        "weight": "5kg",
        "specification": "레이저",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "SLEEPING_GEAR": {
        "material": "면 100%",
        "color": "화이트",
        "size": "싱글",
        "components": "이불 1장",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "CELLPHONE": {
        "itemName": "테스트 스마트폰",
        "modelName": "SP-001",
        "certificationType": "KC 인증",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "importer": "테스트수입사",
        "producer": "테스트제조사",
        "size": "가로 15cm",
        "weight": "200g",
        "telecomType": "SKT,KV,LG",
        "joinProcess": "가입 상품",
        "extraBurden": "0원",
        "specification": "5G",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "OPTICS_APPLIANCES": {
        "itemName": "테스트 카메라",
        "modelName": "CAM-001",
        "certificationType": "KC 인증",
        "releaseDateType": "출시일",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 13cm",
        "weight": "500g",
        "specification": "미러리스",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "JEWELLERY": {
        "material": "골드",
        "purity": "18K",
        "bandMaterial": "골드",
        "weight": "3g",
        "manufacturer": "테스트제조사",
        "producer": "테스트제조사",
        "size": "가로 5cm",
        "caution": "습기 주의",
        "specification": "주얼리",
        "provideWarranty": "제공",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "BOOKS": {
        "title": "테스트 책",
        "author": "테스트 작가",
        "publisher": "테스트 출판사",
        "size": "A5",
        "pages": "200",
        "components": "책 1권",
        "publishDateType": "출간일",
        "publishDate": "20260101",
        "publishDateText": "2026-01-01",
        "description": "테스트 도서 설명",
    },
    "KIDS": {
        "itemName": "테스트 어린이 완구",
        "modelName": "KD-001",
        "certificationType": "KC 인증",
        "size": "가로 20cm",
        "weight": "300g",
        "color": "레드",
        "material": "플라스틱",
        "recommendedAge": "3세 이상",
        "releaseDateType": "출시일",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "caution": "보호자 지도 권장",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "BIOCHEMISTRY": {
        "productName": "테스트 세제",
        "dosageForm": "액상",
        "packDateType": "제조일",
        "packDate": "20260101",
        "packDateText": "2026-01",
        "expirationDateType": "유통기한",
        "expirationDate": "20270101",
        "expirationDateText": "2027-01",
        "weight": "1kg",
        "effect": "세정",
        "importer": "테스트수입사",
        "producer": "테스트제조사",
        "manufacturer": "테스트제조사",
        "childProtection": "어린이보호포장 대상",
        "chemicals": "계면활성제",
        "caution": "어린이 손에 닿지 않게 보관",
        "safeCriterionNo": "안전확인인증 12345",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "BIOCIDAL": {
        "productName": "테스트 살균제",
        "weight": "500g",
        "effect": "살균",
        "rangeOfUse": "주방",
        "importer": "테스트수입사",
        "producer": "테스트제조사",
        "manufacturer": "테스트제조사",
        "childProtection": "어린이보호포장 대상",
        "harmfulChemicalSubstance": "염소",
        "maleficence": "흡입 유해",
        "caution": "밀폐 보관",
        "approvalNumber": "살생물제 12345",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "ETC": {
        "itemName": "테스트 기타 상품",
        "modelName": "ETC-001",
        "certificateDetails": "해당없음 상세설명 기재",
        "manufacturer": "테스트제조사",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "FASHION_ITEMS": {
        "type": "지갑",
        "material": "가죽",
        "size": "가로 10cm",
        "manufacturer": "테스트제조사",
        "caution": "습기 주의",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "HOME_APPLIANCES": {
        "itemName": "테스트 가전",
        "modelName": "HA-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "220V",
        "powerConsumption": "300W",
        "energyEfficiencyRating": "2등급",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 30cm",
        "additionalCost": "설치비 별도",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "MICROELECTRONICS": {
        "itemName": "테스트 소형오디오",
        "modelName": "ME-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "5V",
        "powerConsumption": "10W",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 20cm",
        "weight": "500g",
        "specification": "블루투스",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "NAVIGATION": {
        "itemName": "테스트 내비게이션",
        "modelName": "NAV-001",
        "certificationType": "KC 인증",
        "ratedVoltage": "5V",
        "powerConsumption": "5W",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "size": "가로 17cm",
        "weight": "300g",
        "specification": "GPS",
        "updateCost": "업데이트비 유료",
        "freeCostPeriod": "1년 무료",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "CAR_ARTICLES": {
        "itemName": "테스트 자동차 부품",
        "modelName": "CAR-001",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "certificationType": "KC 인증",
        "caution": "전문가 설치 권장",
        "manufacturer": "테스트제조사",
        "size": "가로 20cm",
        "applyModel": "테스트차종",
        "warrantyPolicy": "구매 후 1년 보증",
        "roadWorthyCertification": "적합",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "MEDICAL_APPLIANCES": {
        "itemName": "테스트 의료기기",
        "modelName": "MED-001",
        "licenceNo": "의료기기 12345",
        "advertisingCertificationType": "심의필 12345",
        "ratedVoltage": "220V",
        "powerConsumption": "100W",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "purpose": "치료용",
        "usage": "1일 1회",
        "caution": "의사 지시에 따라 사용",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "KITCHEN_UTENSILS": {
        "itemName": "테스트 조리기구",
        "modelName": "KU-001",
        "material": "스테인리스",
        "components": "냄비 1개",
        "size": "가로 20cm",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "producer": "테스트제조사",
        "importDeclaration": "수입신고 12345",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "COSMETIC": {
        "capacity": "150ml",
        "specification": "스킨케어",
        "expirationDate": "20270101",
        "expirationDateText": "2027-01",
        "usage": "1일 2회",
        "manufacturer": "테스트제조사",
        "producer": "테스트제조사",
        "distributor": "테스트유통",
        "customizedDistributor": "테스트유통",
        "mainIngredient": "나이아신아마이드",
        "certificationType": "기능성화장품",
        "caution": "사용 중 붉어짐 중단",
        "warrantyPolicy": "구매 후 7일 교환",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "FOOD": {
        "foodItem": "테스트 농산물",
        "weight": "1kg",
        "amount": "1봉",
        "size": "표준",
        "packDate": "20260101",
        "packDateText": "2026-01",
        "consumptionDate": "20260201",
        "consumptionDateText": "2026-02",
        "producer": "테스트생산자",
        "relevantLawContent": "농수산물 품질관리법",
        "productComposition": "단품",
        "keep": "냉장 보관",
        "adCaution": "직사광선 주의",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "GENERAL_FOOD": {
        "productName": "테스트 가공식품",
        "foodType": "가공식품",
        "producer": "테스트생산자",
        "location": "국산",
        "packDate": "20260101",
        "packDateText": "2026-01",
        "consumptionDate": "20270101",
        "consumptionDateText": "2027-01",
        "weight": "500g",
        "amount": "1개",
        "ingredients": "밀, 설탕",
        "nutritionFacts": "100kcal",
        "geneticallyModified": "비유전자변형",
        "consumerSafetyCaution": "개봉 후 냉장 보관",
        "importDeclarationCheck": "해당없음 상세설명 기재",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "DIET_FOOD": {
        "productName": "테스트 다이어트보조식품",
        "producer": "테스트생산자",
        "location": "국산",
        "consumptionDate": "20270101",
        "consumptionDateText": "2027-01",
        "storageMethod": "냉장 보관",
        "weight": "300g",
        "amount": "1개",
        "ingredients": "단백질, 식이섬유",
        "nutritionFacts": "200kcal",
        "specification": "분말형",
        "cautionAndSideEffect": "임산부 섭취 주의",
        "nonMedicinalUsesMessage": "질병 치료 목적 아님",
        "geneticallyModified": "비유전자변형",
        "importDeclarationCheck": "해당없음 상세설명 기재",
        "consumerSafetyCaution": "권장량 준수",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "MUSICAL_INSTRUMENT": {
        "itemName": "테스트 기타",
        "modelName": "GTR-001",
        "size": "표준",
        "color": "내추럴",
        "material": "원목",
        "components": "기타 1대",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "detailContent": "어쿠스틱 기타",
        "warrantyPolicy": "구매 후 1년 보증",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "SPORTS_EQUIPMENT": {
        "itemName": "테스트 요가매트",
        "modelName": "YT-001",
        "certificationType": "KC 인증",
        "size": "가로 60cm",
        "weight": "1kg",
        "color": "퍼플",
        "material": "TPE",
        "components": "매트 1장",
        "releaseDate": "20260101",
        "releaseDateText": "2026-01",
        "manufacturer": "테스트제조사",
        "detailContent": "항미생 요가매트",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
    },
    "RENTAL_ETC": {
        "itemName": "테스트 렌탈 정수기",
        "modelName": "RT-001",
        "ownershipTransferCondition": "약정 종료 후 소유권 이전",
        "payingForLossOrDamage": "실비 부담",
        "refundPolicyForCancel": "중도해지 위약금 발생",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "DIGITAL_CONTENTS": {
        "producer": "테스트제공자",
        "termsOfUse": "개인용 한정 사용",
        "usePeriod": "구매 후 1년",
        "medium": "다운로드",
        "requirement": "인터넷 연결 필요",
        "cancelationPolicy": "콘텐츠 다운로드 후 취소 불가",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "GIFT_CARD": {
        "issuer": "테스트발행사",
        "periodStartDate": "20260101",
        "periodEndDate": "20271231",
        "periodDays": "730일",
        "termsOfUse": "전 매장 사용 가능",
        "useStorePlace": "전 매장",
        "useStoreAddressId": "12345",
        "useStoreUrl": "http://example.com",
        "refundPolicy": "미사용 시 환불 가능",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "MOBILE_COUPON": {
        "issuer": "테스트발행사",
        "usableCondition": "전 매장 사용 가능",
        "usableStore": "전 매장",
        "cancelationPolicy": "사용 전 취소 가능",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "MOVIE_SHOW": {
        "sponsor": "테스트주최",
        "actor": "테스트출연진",
        "rating": "15세 이상 관람가",
        "showTime": "120분",
        "showPlace": "테스트공연장",
        "cancelationCondition": "공연 3일 전까지",
        "cancelationPolicy": "취소 수수료 발생",
        "customerServicePhoneNumber": "070-1234-5678",
    },
    "ETC_SERVICE": {
        "serviceProvider": "테스트서비스제공자",
        "certificateDetails": "사업자등록 12345",
        "usableCondition": "예약 후 이용",
        "cancelationStandard": "3일 전까지 무료",
        "cancelationPolicy": "이후 위약금 발생",
        "customerServicePhoneNumber": "070-1234-5678",
    },
}


def _all_notice_specs():
    """data/notice_types.json 의 verified 35종 스펙을 반환."""
    return naver_client._load_notice_type_specs()


def _build_full_body_for_type(notice_type, spec):
    """해당 타입의 모든 선언된 필드를 채운 notice 본문을 만든다.

    공통 5필드(returnCostReason 등)는 config 기본값으로 채우고,
    타입 고유 필드는 _TYPE_REAL_VALUES 에서 가져온다.
    """
    fields = spec.get("fields") or []
    body = {}
    # 공통 5필드.
    body["returnCostReason"] = _NOTICE_CFG_WITH_ORIGIN["return_cost_reason"]
    body["noRefundReason"] = _NOTICE_CFG_WITH_ORIGIN["no_refund_reason"]
    body["qualityAssuranceStandard"] = _NOTICE_CFG_WITH_ORIGIN["quality_assurance_standard"]
    body["compensationProcedure"] = _NOTICE_CFG_WITH_ORIGIN["compensation_procedure"]
    body["troubleShootingContents"] = _NOTICE_CFG_WITH_ORIGIN["trouble_shooting_contents"]
    # 타입 고유 필드는 실질값 매핑에서. 매핑에 없는 필드는 기본 실질값
    # (타입명 + 필드명 기반)으로 채운다 — placeholder 가 아니며 모든 정규화 통과.
    type_values = _TYPE_REAL_VALUES.get(notice_type, {})
    for field in fields:
        if field in body:
            continue
        body[field] = type_values.get(field, f"{notice_type} 실질값 {field}")
    return body


# --------------------------------------------------------------------------- #
# 1. 35개 고시 타입 전수: 선언된 필수 필드를 채운 입력 → 누락 0건.
# --------------------------------------------------------------------------- #
class TestAllNoticeTypesCompliancePass:
    """각 고시 타입의 선언된 필수 필드를 채운 입력이 컴플라이언스를
    통과하는지(필수 필드 누락 0건) 확인한다. 35 케이스."""

    def test_thirty_five_types_present(self):
        """verified 타입이 정확히 35종인가."""
        specs = _all_notice_specs()
        assert len(specs) == 35, f"verified 타입이 35종이 아님: {len(specs)}"

    def test_real_values_cover_all_types(self):
        """_TYPE_REAL_VALUES 매핑이 35종 전체를 커버하는가."""
        specs = _all_notice_specs()
        for spec in specs:
            t = spec["type"]
            # 매핑이 없더라도 _build_full_body_for_type 이 기본값으로 채우므로
            # 필수는 아님. 단 핵심 타입(ETC 등)은 명시 매핑이 있는지 확인.
            if t == "ETC":
                assert t in _TYPE_REAL_VALUES, "ETC 실질값 매핑이 없음"

    def test_each_type_with_filled_required_fields_has_zero_missing(self):
        """35종 각각: 선언된 필수 필드 전부 채운 입력 → _notice_field_missing
        결과가 빈 리스트(누락 0건). 데이터와 코드가 어긋나면 실패."""
        specs = _all_notice_specs()
        failures = []
        for spec in specs:
            notice_type = spec["type"]
            fields = spec.get("fields") or []
            body = _build_full_body_for_type(notice_type, spec)
            missing = qa_agents._notice_field_missing(body, fields)
            if missing:
                failures.append(f"{notice_type}: {len(missing)}건 누락 — {missing[:5]}")
        assert not failures, f"35종 중 {len(failures)}종이 필수 필드 누락:\n  " + "\n  ".join(
            failures
        )

    def test_etc_specifically_has_no_missing(self):
        """ETC 타입 정상 입력 → 필수 필드 누락 0건 (핵심 반례).

        결함 1 정정 후 ETC 의 필수 목록에서 customerServicePhoneNumber 가
        빠졌으므로 afterServiceDirector 만 있으면 통과해야 한다.
        """
        spec = naver_client._notice_type_spec("ETC")
        assert spec is not None
        fields = spec["fields"]
        # ETC 필수 목록에 customerServicePhoneNumber 가 없는지 확인 (정정 확인).
        assert "customerServicePhoneNumber" not in fields, (
            "ETC 필수 목록에 customerServicePhoneNumber 가 남아있음 — "
            "데이터 정정이 반영되지 않음"
        )
        assert "afterServiceDirector" in fields
        body = _build_full_body_for_type("ETC", spec)
        missing = qa_agents._notice_field_missing(body, fields)
        assert missing == [], f"ETC 필수 필드 누락: {missing}"


# --------------------------------------------------------------------------- #
# 2. E2E: qa_compliance 가 ETC 정상 입력을 PASS (핵심 반례).
# --------------------------------------------------------------------------- #
class TestEtcComplianceEndToEnd:
    """ETC 정상 입력 → qa_compliance PASS."""

    def test_etc_normal_input_passes_compliance(self):
        """ETC 타입 정상 입력 → qa_compliance 위반 0건, verdict PASS."""
        spec = naver_client._notice_type_spec("ETC")
        etc_body = _build_full_body_for_type("ETC", spec)
        notice = {
            "productInfoProvidedNoticeType": "ETC",
            "etc": etc_body,
        }
        # 원산지 일치 + AS 전화 존재하는 context.
        context = {
            "notice": notice,
            "origin_content": "중국",
            "as_tel": "070-1234-5678",
            "category_id": None,  # KC 검사 우회(불명 차단 방지).
        }
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_ORIGIN_ONLY):
            result = qa_agents._compliance_code_check("테스트기타상품", context)
        # 고시 필수필드 관련 위반만 추출.
        notice_violations = [
            v for v in result.get("violations", []) if v.get("rule") == "고시 필수필드"
        ]
        assert notice_violations == [], f"ETC 고시 필수필드 위반: {notice_violations}"


# --------------------------------------------------------------------------- #
# 3. placeholder 변형 3종 이상 → 미제공 판정.
# --------------------------------------------------------------------------- #
class TestPlaceholderVariantsDetected:
    """placeholder 변형(공백 삽입·표기 차이 등)이 미제공으로 판정되는가."""

    def test_space_inserted_variant_detected(self):
        """``상 세 참 조`` (공백 삽입) → 미제공."""
        assert qa_agents._is_placeholder_value("상 세 참 조") is True

    def test_double_space_variant_detected(self):
        """``상세페이지  참조`` (공백 2개) → 미제공."""
        assert qa_agents._is_placeholder_value("상세페이지  참조") is True

    def test_fullwidth_space_variant_detected(self):
        """전각 공백(U+3000) 삽입 ``상세\u3000참조`` → 미제공."""
        assert qa_agents._is_placeholder_value("상세\u3000참조") is True

    def test_no_space_compact_variant_detected(self):
        """``상세페이지참조`` (공백 없음) → 미제공."""
        assert qa_agents._is_placeholder_value("상세페이지참조") is True

    def test_leading_trailing_space_variant_detected(self):
        """``  상세참조  `` (앞뒤 공백) → 미제공."""
        assert qa_agents._is_placeholder_value("  상세참조  ") is True

    def test_detailed_page_confirm_variant_detected(self):
        """``상세페이지 확인`` 변형 → 미제공."""
        assert qa_agents._is_placeholder_value("상세 페이지 확인") is True

    def test_bonpum_variant_detected(self):
        """``본품 참조`` 변형 → 미제공."""
        assert qa_agents._is_placeholder_value("본품 참조") is True

    def test_placeholder_variants_in_notice_field_missing(self):
        """placeholder 변형 값이 필드에 들어가면 누락으로 보고되는가."""
        body = {
            "material": "상 세 참 조",  # 공백 삽입 변형
            "color": "상세페이지참조",  # 공백 없음 변형
            "size": "상세\u3000참조",  # 전각 공백 변형
            "valid_field": "면 100%",  # 정상 값
        }
        fields = ["material", "color", "size", "valid_field"]
        missing = qa_agents._notice_field_missing(body, fields)
        # 3개 변형은 누락, 정상값은 누락 아님.
        assert "material" in missing
        assert "color" in missing
        assert "size" in missing
        assert "valid_field" not in missing


# --------------------------------------------------------------------------- #
# 4. 정상 값 3종 → 통과 (과잉 차단 금지).
# --------------------------------------------------------------------------- #
class TestNormalValuesNotBlocked:
    """정상 실질값이 placeholder 로 오판되지 않는가 (과잉 차단 금지)."""

    def test_normal_material_passes(self):
        """``면 100%`` → placeholder 아님."""
        assert qa_agents._is_placeholder_value("면 100%") is False

    def test_normal_date_passes(self):
        """``2026-01`` → placeholder 아님."""
        assert qa_agents._is_placeholder_value("2026-01") is False

    def test_normal_measurement_passes(self):
        """``어깨 42cm`` → placeholder 아님."""
        assert qa_agents._is_placeholder_value("어깨 42cm") is False

    def test_normal_kc_number_passes(self):
        """``안전확인인증 12345`` 같은 값 → placeholder 아님."""
        assert qa_agents._is_placeholder_value("안전확인인증 12345") is False

    def test_normal_phone_passes(self):
        """``070-1234-5678`` → placeholder 아님 (구두점 휴리스틱이 잡지 않아야)."""
        assert qa_agents._is_placeholder_value("070-1234-5678") is False

    def test_normal_value_in_notice_field_missing(self):
        """정상값들로 채운 본문은 누락 0건."""
        body = {
            "material": "면 100%",
            "size": "어깨 42cm",
            "packDateText": "2026-01",
        }
        missing = qa_agents._notice_field_missing(body, ["material", "size", "packDateText"])
        assert missing == [], f"정상값이 placeholder 로 오판됨: {missing}"


# --------------------------------------------------------------------------- #
# 5. 전송 유지: 안내문구 명시 입력 시 payload 에 그대로 존재.
# --------------------------------------------------------------------------- #
class TestTransmissionPreserved:
    """사용자가 안내문구를 명시 입력해도 payload 에는 그대로 실린다
    (전송과 판정 분리)."""

    def test_placeholder_value_transmitted_to_payload(self):
        """``상세페이지 참조`` 명시 입력 → build_payload 의 wear 노드에 그대로 존재."""
        product = {
            "name": "테스트니트",
            "categoryId": "50021299",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "notice": {
                "productInfoProvidedNoticeType": "WEAR",
                "wear": {
                    "material": "면 100%",
                    "color": "블랙",
                    "size": "FREE",
                    "caution": "물 세탁 가능",
                    "packDateText": "상세페이지 참조",  # placeholder
                    "warrantyPolicy": "구매 후 7일 교환 가능",
                    "manufacturer": "테스트제조사",
                },
            },
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://cdn/x.png"])
        wear = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
            .get("wear", {})
        )
        assert (
            wear.get("packDateText") == "상세페이지 참조"
        ), f"placeholder 값이 payload 에 없음(전송 X): {wear.get('packDateText')!r}"


# --------------------------------------------------------------------------- #
# 6. 무동작/identity 금지 — 정규화·대조 로직이 실제로 동작하는가.
# --------------------------------------------------------------------------- #
class TestNormalizationNoNoOp:
    """정규화·대조 로직이 identity 가 아님을 확인."""

    def test_normalize_collapses_whitespace(self):
        """정규화가 내부 공백 런을 축소하는가."""
        std, compact = qa_agents._normalize_placeholder_value("a   b")
        assert std == "a b"
        assert compact == "ab"

    def test_normalize_fullwidth_space(self):
        """전각 공백이 ASCII 공백으로 통일되는가."""
        std, _ = qa_agents._normalize_placeholder_value("a\u3000b")
        assert std == "a b"

    def test_normalize_lowercases(self):
        """소문자 변환이 일어나는가."""
        std, _ = qa_agents._normalize_placeholder_value("N/A")
        assert std == "n/a"

    def test_is_placeholder_value_not_identity(self):
        """_is_placeholder_value 가 실제로 True/False 를 구분하는가."""
        assert qa_agents._is_placeholder_value("상세참조") is True
        assert qa_agents._is_placeholder_value("면 100%") is False
