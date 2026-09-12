# -*- coding: utf-8 -*-
"""
tests/test_social_insurance.py — 4대보험 요율·보험료·감면 골든 테스트

각 케이스는 2026-09-12에 law.go.kr에서 법령·고시 원문을 읽어 확정한 값을 고정한다.
여기서 깨지면 요율표가 현행 법령과 어긋났거나 계산식이 뒤틀린 것이다.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import social_insurance as si  # noqa: E402


# ---------------------------------------------------------------------------
# 요율표 — 법령 원문에서 확인한 값
# ---------------------------------------------------------------------------

def test_health_rate_by_year():
    """국민건강보험법 시행령 §44① — 2023~2025년 7.09%가 이어지다 2026년 7.19%로."""
    assert si.health_rate(2023) == 0.0709
    assert si.health_rate(2025) == 0.0709
    assert si.health_rate(2026) == 0.0719


def test_pension_rate_uses_addenda_not_main_text():
    """국민연금 요율은 본칙(각 6.5%)이 아니라 부칙 제4조 특례로 단계 인상된다.

    2025년까지 4.5% → 2026년 4.75% → 매년 0.25%p → 2032년 6.25% → 2033년 본칙 6.5%.
    본칙만 읽고 2026년에 6.5%를 쓰면 1인당 매월 수만 원이 틀어진다.
    """
    assert si.pension_rate(2025) == 0.045
    assert si.pension_rate(2026) == 0.0475
    assert si.pension_rate(2032) == 0.0625
    assert si.pension_rate(2033) == 0.065
    assert si.pension_rate(2040) == 0.065


def test_ltc_ratio_base_change_in_2023():
    """장기요양 요율 표기가 2023년에 '건강보험료 대비'→'소득 대비'로 바뀌었다.

    건강보험료에 곱할 비율은 (장기요양요율 ÷ 건강보험료율), 소수 다섯째자리에서 반올림
    (노인장기요양보험법 §9①). 2025년 12.95%, 2026년 13.14%가 공표값과 같아야 한다.
    """
    assert si.ltc_ratio(2022) == 0.1227          # 2022년까지는 시행령 값 그대로
    assert si.ltc_ratio(2025) == 0.1295
    assert si.ltc_ratio(2026) == 0.1314


def test_pension_band_applies_july_to_june():
    """기준소득월액 상·하한의 적용기간은 7월~다음 해 6월 (시행령 §5④)."""
    assert si.pension_income_band("2026-06-30") == (400_000, 6_370_000, 2025)
    assert si.pension_income_band("2026-07-01") == (410_000, 6_590_000, 2026)


def test_ei_rate_changes_mid_year():
    """실업급여 요율은 연중에 바뀐다 — 연도 키로 잡으면 2019·2022년이 틀린다."""
    assert si.ei_unemployment_rate("2019-09-30") == 0.013
    assert si.ei_unemployment_rate("2019-10-01") == 0.016
    assert si.ei_unemployment_rate("2022-06-30") == 0.016
    assert si.ei_unemployment_rate("2022-07-01") == 0.018


def test_unknown_year_raises_instead_of_guessing():
    """표에 없는 연도는 조용히 최신값을 쓰지 않는다 — 갱신을 강제한다."""
    with pytest.raises(ValueError, match="파라미터 표에 없습니다"):
        si.health_rate(2015)
    with pytest.raises(ValueError, match="파라미터 표에 없습니다"):
        si.pension_income_band("2009-08-01")


# ---------------------------------------------------------------------------
# 보험료 계산
# ---------------------------------------------------------------------------

def test_premium_2025_matches_published_figures():
    """2025년 보수월액 300만원 — 공단 공표 계산과 일치해야 한다."""
    r = si.calc_social_insurance(3_000_000, "2025-09-01")["결과"]
    assert r["근로자부담"]["건강보험"] == 106_350        # 300만 × 7.09% ÷ 2
    assert r["근로자부담"]["장기요양"] == 13_770         # 212,700 × 12.95% ÷ 2
    assert r["근로자부담"]["국민연금"] == 135_000        # 300만 × 4.5%
    assert r["근로자부담"]["고용보험"] == 27_000         # 300만 × 0.9%
    assert r["사업주부담"]["고용보험_고용안정직능"] == 7_500   # 300만 × 0.25%


def test_premium_2026_reflects_new_rates():
    r = si.calc_social_insurance(3_000_000, "2026-09-01")["결과"]
    assert r["근로자부담"]["건강보험"] == 107_850        # 7.19%
    assert r["근로자부담"]["장기요양"] == 14_170         # 215,700 × 13.14% ÷ 2
    assert r["근로자부담"]["국민연금"] == 142_500        # 4.75% (부칙 특례)


def test_no_floating_point_dust_in_floor10():
    """3,000,000 × 0.018 ÷ 2 는 float로 26999.999…가 되어 절사하면 26,990원이 된다.

    Decimal로 계산하지 않으면 고용보험료가 매번 10원씩 모자란다 — 실제로 겪은 버그다.
    """
    assert si._floor10(3_000_000 * 0.018 / 2) == 27_000
    for wage in (1_234_567, 2_777_777, 4_321_000, 9_999_999):
        r = si.calc_social_insurance(wage, "2026-01-01")["결과"]
        for part in ("근로자부담", "사업주부담"):
            for k, v in r[part].items():
                assert v % 10 == 0, f"{part}.{k}={v} — 10원 단위가 아니다"


def test_caps_and_floors_apply():
    """건강보험료 상·하한과 연금 기준소득월액 상·하한."""
    low = si.calc_social_insurance(200_000, "2026-09-01")["결과"]
    assert low["근로자부담"]["건강보험"] == 10_080      # 하한 20,160원의 1/2
    assert low["근로자부담"]["국민연금"] == 19_470      # 하한 410,000 × 4.75%
    high = si.calc_social_insurance(100_000_000, "2026-09-01")["결과"]
    assert high["근로자부담"]["국민연금"] == 313_020    # 상한 6,590,000 × 4.75%
    off = si.calc_social_insurance(200_000, "2026-09-01", apply_caps=False)["결과"]
    assert off["근로자부담"]["국민연금"] == 9_500       # 200,000 × 4.75%


def test_industrial_accident_optional_and_warned():
    r = si.calc_social_insurance(3_000_000, "2026-01-01")
    assert r["결과"]["사업주부담"]["산재보험"] == 0
    assert any("산재보험료" in w for w in r["주의사항"])
    r2 = si.calc_social_insurance(3_000_000, "2026-01-01", industrial_accident_rate=0.0085)
    assert r2["결과"]["사업주부담"]["산재보험"] == 25_500


def test_pension_step_up_is_flagged_in_warnings():
    r = si.calc_social_insurance(3_000_000, "2026-01-01")
    assert any("부칙" in w and "4.75%" in w for w in r["주의사항"])


def test_negative_wage_rejected():
    with pytest.raises(ValueError):
        si.calc_social_insurance(-1, "2026-01-01")


# ---------------------------------------------------------------------------
# 두루누리 지원
# ---------------------------------------------------------------------------

def test_support_eligible_case():
    r = si.check_premium_support(2_000_000, 5, as_of="2026-09-01",
                                 property_tax_base=100_000_000, global_income=30_000_000)["결과"]
    assert r["지원대상"] is True and r["판정"] == "대상"
    assert r["월지원액"]["고용보험_근로자분"] == 14_400   # 18,000 × 80%
    assert r["월지원액"]["고용보험_사업주분"] == 18_400   # (18,000+5,000) × 80%
    assert r["월지원액"]["연금_근로자분"] == 76_000       # 95,000 × 80%


def test_support_caps_bind():
    """월 보수가 한도 근처면 고시의 월 한도가 걸린다."""
    r = si.check_premium_support(2_690_000, 3, as_of="2026-09-01",
                                 property_tax_base=1, global_income=1)["결과"]
    assert r["월지원액"]["고용보험_근로자분"] <= si.PREMIUM_SUPPORT["고용보험_월한도_근로자"]
    assert r["월지원액"]["고용보험_사업주분"] <= si.PREMIUM_SUPPORT["고용보험_월한도_사업주"]
    assert r["월지원액"]["연금_근로자분"] <= si.PREMIUM_SUPPORT["연금_월한도"]


def test_support_rejects_on_each_hard_requirement():
    base = dict(as_of="2026-09-01", property_tax_base=1, global_income=1)
    assert si.check_premium_support(2_000_000, 10, **base)["결과"]["지원대상"] is False   # 10명
    assert si.check_premium_support(2_700_000, 5, **base)["결과"]["지원대상"] is False    # 270만
    assert si.check_premium_support(2_000_000, 5, newly_insured=False,
                                    **base)["결과"]["지원대상"] is False                 # 신규 아님
    assert si.check_premium_support(2_000_000, 5, supported_months=36,
                                    **base)["결과"]["지원대상"] is False                 # 36개월 소진


def test_support_does_not_assume_unknown_requirements_are_met():
    """재산·종합소득을 모르면 '대상'이라고 단정하지 않는다."""
    r = si.check_premium_support(2_000_000, 5, as_of="2026-09-01")["결과"]
    assert r["지원대상"] is None
    assert r["판정"].startswith("판정보류")


def test_owner_excluded_from_pension_support():
    r = si.check_premium_support(2_000_000, 5, as_of="2026-09-01", is_owner_or_ceo=True,
                                 property_tax_base=1, global_income=1)["결과"]
    assert r["월지원액"]["연금_근로자분"] == 0 and r["월지원액"]["연금_사업주분"] == 0
    assert r["월지원액"]["고용보험_근로자분"] > 0


def test_health_insurance_not_supported_is_stated():
    r = si.check_premium_support(2_000_000, 5, as_of="2026-09-01")
    assert any("건강보험료" in w and "대상이 아닙니다" in w for w in r["주의사항"])


# ---------------------------------------------------------------------------
# 원천 대조 (네트워크)
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_rate_table_matches_current_law():
    """하드코딩한 요율이 현행 법령과 같은지 — 해마다 이게 먼저 깨진다."""
    out = si.verify_rates(date.today().year)
    checked = [i for i in out["항목"] if i["일치"] is not None]
    if not checked:
        pytest.skip(f"law.go.kr 조회 실패로 대조 못 함: {out['상태']}")
    bad = [i for i in checked if not i["일치"]]
    assert not bad, f"요율표가 현행 법령과 다릅니다: {bad}"
