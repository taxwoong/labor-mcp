# -*- coding: utf-8 -*-
"""
tests/test_calculators.py — 노무 계산 엔진 함정 골든 테스트

각 케이스는 개발계획.md 리서치에서 확인된 실무 함정(판례·행정해석 변경, 연도별
산입비율, 5인 미만 분기 등)을 고정한다. 여기서 깨지면 법리 반영이 뒤틀린 것.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labor_constants as lc
from calculators import (
    calc_annual_leave,
    calc_average_wage,
    calc_dismissal_notice_pay,
    calc_ordinary_wage,
    calc_overtime_pay,
    calc_severance_pay,
    calc_wage_cut_limit,
    calc_weekly_holiday_pay,
    check_minimum_wage,
    classify_wage_item,
    weekly_52h_check,
)


def item(명칭, 금액, 주기="매월", 성격="기본급", 실비=False, 최소보장=None):
    d = {"명칭": 명칭, "금액": 금액, "지급주기": 주기, "성격": 성격, "실비변상": 실비}
    if 최소보장 is not None:
        d["최소보장액"] = 최소보장
    return d


# ---------------------------------------------------------------------------
# 연차유급휴가 — 2021다227100 + 행정해석 변경(다음 날 재직 요건)
# ---------------------------------------------------------------------------

class TestAnnualLeave:
    def test_1년_기간제_연차는_11일(self):
        # 2023-01-01~2023-12-31 계약 → 퇴직일(마지막 근로일 다음 날) 2024-01-01 미재직
        r = calc_annual_leave("2023-01-01", "2024-01-01", employed_on_base_date=False)
        assert r["결과"]["총발생일수"] == 11
        assert r["결과"]["월개근분"] == 11
        assert r["결과"]["연차분"] == 0  # 365일 근무 후 퇴직 → 15일분 미발생

    def test_365일_근무_후_다음날_재직이면_15일_발생(self):
        r = calc_annual_leave("2023-01-01", "2024-01-01", employed_on_base_date=True)
        assert r["결과"]["총발생일수"] == 26  # 월개근 11 + 1년차 15
        assert r["결과"]["연차분"] == 15

    def test_3년_근속_가산(self):
        # 2021-01-01 입사, 2024-06-01 기준: 1·2년차 15 + 3년차 16 (§60④)
        r = calc_annual_leave("2021-01-01", "2024-06-01")
        assert r["결과"]["연차분"] == 15 + 15 + 16
        assert r["결과"]["총발생일수"] == 11 + 46

    def test_출근율_80퍼센트_미만이면_연차_미발생(self):
        r = calc_annual_leave("2022-01-01", "2023-06-01", attendance_rate=0.7)
        assert r["결과"]["연차분"] == 0
        assert r["결과"]["총발생일수"] == 11  # 월개근분만

    def test_회계연도_모드_비례연차와_퇴직정산_비교(self):
        # 2023-07-01 입사, 회계연도 1/1: 2024-01-01에 비례 15×184/365=7.6일
        r = calc_annual_leave("2023-07-01", "2024-01-01", mode="회계연도",
                              fiscal_start="01-01", employed_on_base_date=True)
        assert r["결과"]["월개근분"] == 6
        assert r["결과"]["회계연도기준총일수"] == pytest.approx(13.6)
        assert r["결과"]["입사일기준총일수"] == 6
        # 근로자에게 유리한(많은) 쪽이 보장 기준
        assert r["결과"]["보장기준일수"] == pytest.approx(13.6)

    def test_5인_미만_미적용_주의문구(self):
        r = calc_annual_leave("2023-01-01", "2024-06-01")
        assert any("5인 미만" in s for s in r["주의사항"])


# ---------------------------------------------------------------------------
# 임금항목 3축 판정 — 통상임금 신법리 × 최저임금 산입범위
# ---------------------------------------------------------------------------

class TestClassifyWageItem:
    def test_재직조건부_매월_정기상여는_통상임금_포함(self):
        # 신법리(2024-12-19 전합): 재직조건·최소근무일수 조건 무관 — 스키마에 재직조건
        # 필드 자체가 없는 이유. 정기상여면 무조건 포함으로 판정되어야 한다.
        cls = classify_wage_item(item("정기상여", 300_000, "매월", "정기상여"), 2026)
        assert cls["통상임금"] == "포함"
        assert any("2020다247190" in g for g in cls["근거"])

    def test_격월_상여는_최저임금_불산입_통상임금은_포함(self):
        cls = classify_wage_item(item("상여금", 600_000, "격월", "정기상여"), 2026)
        assert cls["최저임금산입"].startswith("불산입")
        assert cls["통상임금"] == "포함"  # 정기성은 지급 주기와 무관
        assert cls["평균임금"] == "포함"

    def test_매월_식대는_2026년_최저임금_전액_산입(self):
        cls = classify_wage_item(item("식대", 200_000, "매월", "식대"), 2026)
        assert cls["최저임금산입"] == "산입"
        assert cls["통상임금"] == "포함"

    def test_2022년_매월_상여식대는_부분산입_판정(self):
        assert classify_wage_item(
            item("상여", 300_000, "매월", "정기상여"), 2022
        )["최저임금산입"].startswith("부분산입")
        assert classify_wage_item(
            item("식대", 100_000, "매월", "식대"), 2022
        )["최저임금산입"].startswith("부분산입")

    def test_실비변상_출장비는_3축_모두_제외(self):
        cls = classify_wage_item(item("출장비", 150_000, "매월", "기타", 실비=True), 2026)
        assert cls["최저임금산입"].startswith("불산입")
        assert cls["통상임금"].startswith("제외")
        assert cls["평균임금"].startswith("제외")

    def test_연장수당은_최저임금_불산입_통상임금_제외(self):
        cls = classify_wage_item(item("연장수당", 200_000, "매월", "연장수당"), 2026)
        assert cls["최저임금산입"].startswith("불산입")
        assert cls["통상임금"].startswith("제외")
        assert cls["평균임금"] == "포함"  # 임금성은 있음

    def test_성과급은_최소보장액만_통상임금(self):
        cls = classify_wage_item(item("성과급", 500_000, "매월", "성과급", 최소보장=200_000), 2026)
        assert cls["통상임금"] == "포함(최소보장액 한정)"
        assert cls["통상임금산입액"] == 200_000
        assert classify_wage_item(
            item("성과급", 500_000, "매월", "성과급"), 2026
        )["통상임금"].startswith("제외")

    def test_가족수당은_불확실(self):
        cls = classify_wage_item(item("가족수당", 50_000, "매월", "가족수당"), 2026)
        assert cls["통상임금"].startswith("불확실")


# ---------------------------------------------------------------------------
# 통상임금 월액·시급
# ---------------------------------------------------------------------------

class TestOrdinaryWage:
    def test_격월상여_월환산_포함(self):
        items = [item("기본급", 2_000_000), item("상여금", 600_000, "격월", "정기상여"),
                 item("식대", 100_000, 성격="식대")]
        r = calc_ordinary_wage(items, 40, 2026)
        assert r["결과"]["월통상임금"] == 2_400_000  # 2,000,000 + 600,000/2 + 100,000
        assert r["결과"]["월기준시간"] == 209.0
        assert r["결과"]["통상시급"] == pytest.approx(2_400_000 / 209, abs=0.01)

    def test_가족수당은_불확실로_제외되고_주의사항에_표시(self):
        items = [item("기본급", 2_000_000), item("가족수당", 50_000, 성격="가족수당")]
        r = calc_ordinary_wage(items, 40, 2026)
        assert r["결과"]["월통상임금"] == 2_000_000
        assert "가족수당" in r["결과"]["불확실항목"]
        assert any("가족수당" in s for s in r["주의사항"])

    def test_성과급_최소보장액만_산입(self):
        items = [item("기본급", 2_000_000),
                 item("성과급", 500_000, 성격="성과급", 최소보장=200_000)]
        r = calc_ordinary_wage(items, 40, 2026)
        assert r["결과"]["월통상임금"] == 2_200_000

    def test_2024_전합_이전_산정시점이면_구법리_주의문구(self):
        r = calc_ordinary_wage([item("기본급", 2_000_000)], 40, 2024, as_of_date="2024-06-01")
        assert any("구법리" in s for s in r["주의사항"])
        # 판결일 이후 산정분에는 구법리 문구가 없어야 함
        r2 = calc_ordinary_wage([item("기본급", 2_000_000)], 40, 2025, as_of_date="2025-01-01")
        assert not any("구법리" in s for s in r2["주의사항"])


# ---------------------------------------------------------------------------
# 최저임금 검증 — 연도별 산입비율·수습 3요건·미등록 연도
# ---------------------------------------------------------------------------

class TestMinimumWage:
    def test_2026년_최저임금_월환산액_정확_일치(self):
        r = check_minimum_wage([item("기본급", 2_156_880)], 40, 2026)
        assert r["결과"]["최저시급"] == 10_320
        assert r["결과"]["월환산최저임금"] == 2_156_880
        assert r["결과"]["비교시급"] == pytest.approx(10_320.0)
        assert r["결과"]["위반여부"] is False

    def test_2022년_상여10_복리후생2_비율_적용(self):
        # 월 환산 최저임금 9,160×209 = 1,914,440원
        # 상여 임계 10% = 191,444 → 300,000 중 108,556 산입
        # 식대 임계 2% = 38,288.8 → 100,000 중 61,711.2 산입
        items = [item("기본급", 1_800_000),
                 item("정기상여", 300_000, 성격="정기상여"),
                 item("식대", 100_000, 성격="식대")]
        r = check_minimum_wage(items, 40, 2022)
        assert r["결과"]["정기상여_산입액"] == pytest.approx(108_556.0)
        assert r["결과"]["복리후생_산입액"] == pytest.approx(61_711.2)
        assert r["결과"]["산입액합계"] == pytest.approx(1_970_267.2)
        assert r["결과"]["위반여부"] is False  # 비교시급 9,427 > 9,160

    def test_2026년은_상여_식대_전액_산입(self):
        items = [item("기본급", 1_800_000),
                 item("정기상여", 300_000, 성격="정기상여"),
                 item("식대", 100_000, 성격="식대")]
        r = check_minimum_wage(items, 40, 2026)
        assert r["결과"]["정기상여_산입액"] == 0  # 부분산입 그룹이 아예 없음
        assert r["결과"]["전액산입합계"] == 2_200_000
        assert r["결과"]["산입액합계"] == 2_200_000

    def test_격월_상여는_산입액에서_빠진다(self):
        items = [item("기본급", 2_156_880), item("상여금", 1_000_000, "격월", "정기상여")]
        r = check_minimum_wage(items, 40, 2026)
        assert r["결과"]["산입액합계"] == 2_156_880

    def test_수습_3요건_충족시_10퍼센트_감액_유효(self):
        r = check_minimum_wage([item("기본급", 2_000_000)], 40, 2026,
                               probation=True, contract_1yr_plus=True, simple_labor=False)
        assert r["결과"]["수습감액적용"] is True
        assert r["결과"]["적용최저시급"] == pytest.approx(10_320 * 0.9)
        assert r["결과"]["위반여부"] is False  # 9,569.4 ≥ 9,288

    def test_1년_미만_계약은_수습이어도_감액_불가(self):
        r = check_minimum_wage([item("기본급", 2_000_000)], 40, 2026,
                               probation=True, contract_1yr_plus=False)
        assert r["결과"]["수습감액적용"] is False
        assert r["결과"]["적용최저시급"] == 10_320
        assert r["결과"]["위반여부"] is True
        assert r["결과"]["월부족액"] == pytest.approx(156_880)  # 2,156,880 − 2,000,000

    def test_단순노무직은_수습이어도_감액_불가(self):
        r = check_minimum_wage([item("기본급", 2_000_000)], 40, 2026,
                               probation=True, contract_1yr_plus=True, simple_labor=True)
        assert r["결과"]["수습감액적용"] is False
        assert r["결과"]["위반여부"] is True

    def test_미등록_연도는_명시적_오류(self):
        with pytest.raises(ValueError):
            check_minimum_wage([item("기본급", 2_000_000)], 40, 2027)
        with pytest.raises(ValueError):
            lc.minimum_wage(2027)


# ---------------------------------------------------------------------------
# 주휴수당 — 15시간 미만 제외·비례 계산
# ---------------------------------------------------------------------------

class TestWeeklyHolidayPay:
    def test_주_14시간은_주휴_0원(self):
        r = calc_weekly_holiday_pay(14, 10_000)
        assert r["결과"]["주휴수당"] == 0
        assert any("§18③" in g or "15시간" in g for g in r["근거"])

    def test_주_20시간_단시간_비례(self):
        r = calc_weekly_holiday_pay(20, 10_000)
        assert r["결과"]["주휴시간"] == 4.0  # (20/40)×8
        assert r["결과"]["주휴수당"] == 40_000

    def test_주_40시간_전일제(self):
        assert calc_weekly_holiday_pay(40, 10_000)["결과"]["주휴수당"] == 80_000

    def test_개근_아니면_0원(self):
        assert calc_weekly_holiday_pay(40, 10_000, perfect_attendance=False)["결과"]["주휴수당"] == 0


# ---------------------------------------------------------------------------
# 가산수당 — 5인 미만 분기·중복 가산·휴일 8시간 초과
# ---------------------------------------------------------------------------

class TestOvertimePay:
    def test_5인_미만은_연장해도_가산_0(self):
        r = calc_overtime_pay(10_000, overtime_h=10, night_h=0, holiday_h=0, employees=4)
        assert r["결과"]["가산적용"] is False
        assert r["결과"]["합계"] == 100_000  # 근로 시간분 100%만, 가산 없음
        assert any("5인 미만" in s or "4인 이하" in s for s in r["주의사항"])

    def test_5인_이상_연장_야간_중복_가산(self):
        # 연장 2h가 전부 야간과 겹치는 경우: 2×1.5 + 2×0.5 = 시급의 4배
        r = calc_overtime_pay(10_000, overtime_h=2, night_h=2, holiday_h=0, employees=5)
        assert r["결과"]["연장수당"] == 30_000
        assert r["결과"]["야간가산수당"] == 10_000
        assert r["결과"]["합계"] == 40_000

    def test_휴일_10시간은_8시간_1_5배_나머지_2_0배(self):
        r = calc_overtime_pay(10_000, overtime_h=0, night_h=0, holiday_h=10, employees=5)
        assert r["결과"]["휴일수당_8시간이내"] == 120_000  # 8×1.5×10,000
        assert r["결과"]["휴일수당_8시간초과"] == 40_000   # 2×2.0×10,000
        assert r["결과"]["합계"] == 160_000

    def test_포괄임금_주의문구(self):
        r = calc_overtime_pay(10_000, 2, 0, 0, employees=5)
        assert any("포괄임금" in s for s in r["주의사항"])


# ---------------------------------------------------------------------------
# 평균임금 — 연간 항목 3/12·통상임금 하한
# ---------------------------------------------------------------------------

class TestAverageWage:
    def test_연_상여_1200만원이면_300만원만_산입(self):
        months = [[item("기본급", 3_000_000)] for _ in range(3)]
        r = calc_average_wage(months, 12_000_000, 0, 92)
        assert r["결과"]["상여산입액"] == 3_000_000  # 12,000,000 × 3/12
        assert r["결과"]["1일평균임금"] == pytest.approx(12_000_000 / 92, abs=0.01)

    def test_창구_안의_격월상여는_이중산입_방지로_제외(self):
        months = [[item("기본급", 3_000_000), item("상여", 1_000_000, "격월", "정기상여")]
                  for _ in range(3)]
        r = calc_average_wage(months, 6_000_000, 0, 92)
        assert r["결과"]["임금총액"] == 9_000_000 + 6_000_000 * 3 / 12

    def test_평균이_통상보다_낮으면_통상임금_적용(self):
        # 휴업 등으로 3개월 임금이 낮아진 경우
        months = [[item("기본급", 1_500_000)] for _ in range(3)]
        r = calc_average_wage(months, 0, 0, 92, ordinary_wage_monthly=3_000_000,
                              weekly_hours=40)
        daily_ow = 3_000_000 / 209 * 8  # 114,832.54
        assert r["결과"]["통상임금하한적용"] is True
        assert r["결과"]["적용평균임금"] == pytest.approx(daily_ow, abs=0.01)
        assert r["결과"]["1일평균임금"] == pytest.approx(4_500_000 / 92, abs=0.01)

    def test_평균이_통상보다_높으면_평균임금_유지(self):
        months = [[item("기본급", 4_000_000)] for _ in range(3)]
        r = calc_average_wage(months, 0, 0, 92, ordinary_wage_monthly=3_000_000)
        assert r["결과"]["통상임금하한적용"] is False
        assert r["결과"]["적용평균임금"] == r["결과"]["1일평균임금"]


# ---------------------------------------------------------------------------
# 퇴직급여
# ---------------------------------------------------------------------------

class TestSeverancePay:
    def test_2년_근속_퇴직금(self):
        r = calc_severance_pay(100_000, 730, 40)
        assert r["결과"]["퇴직급여"] == pytest.approx(100_000 * 30 * 730 / 365)

    def test_1년_미만은_미발생(self):
        assert calc_severance_pay(100_000, 300, 40)["결과"]["퇴직급여"] == 0

    def test_주_15시간_미만은_미발생(self):
        assert calc_severance_pay(100_000, 730, 14)["결과"]["퇴직급여"] == 0

    def test_DC형은_산정식이_다름을_안내(self):
        r = calc_severance_pay(100_000, 730, 40, plan_type="DC")
        assert r["결과"]["퇴직급여"] is None
        assert "1/12" in r["결과"]["산정방식"]
        assert any("1/12" in s for s in r["주의사항"])

    def test_통상임금_하한_주의문구(self):
        r = calc_severance_pay(100_000, 730, 40)
        assert any("통상임금" in s for s in r["주의사항"])


# ---------------------------------------------------------------------------
# 해고예고수당 · 감급 한도
# ---------------------------------------------------------------------------

class TestDismissalNotice:
    def test_근속_2개월은_예외(self):
        r = calc_dismissal_notice_pay(100_000, 2)
        assert r["결과"]["해고예고수당"] == 0
        assert r["결과"]["예외해당"] is True
        assert "3개월" in r["결과"]["예외사유"]

    def test_30일분_통상임금(self):
        r = calc_dismissal_notice_pay(100_000, 12)
        assert r["결과"]["해고예고수당"] == 3_000_000
        # 해고예고 적법 ≠ 해고 정당성
        assert any("정당" in s for s in r["주의사항"])

    def test_예외사유_있으면_미발생(self):
        r = calc_dismissal_notice_pay(100_000, 12, exempt_reason="천재·사변으로 사업 계속 불가")
        assert r["결과"]["해고예고수당"] == 0

    def test_감급_한도(self):
        r = calc_wage_cut_limit(100_000, 3_000_000)
        assert r["결과"]["1회한도"] == 50_000    # 평균임금 1일분 × 1/2
        assert r["결과"]["총액한도"] == 300_000  # 1임금지급기 총액 × 1/10


# ---------------------------------------------------------------------------
# 주 52시간 검사
# ---------------------------------------------------------------------------

class Test52Hours:
    def test_54시간_주_검출(self):
        r = weekly_52h_check([40, 54, 41])
        assert r["결과"]["판정"] == "위반"
        assert r["결과"]["위반주차"] == [2]
        assert r["결과"]["주별상세"][1]["초과시간"] == 2

    def test_전부_한도_이내면_적법(self):
        assert weekly_52h_check([40, 52, 41])["결과"]["판정"] == "적법"

    def test_탄력근로제는_특정주_초과만으로는_불확실(self):
        r = weekly_52h_check([40, 54, 41], flex_mode="탄력")
        assert r["결과"]["판정"] == "불확실"
        assert any("평균" in s for s in r["주의사항"])

    def test_탄력근로제도_평균_초과면_위반(self):
        r = weekly_52h_check([56, 56, 56], flex_mode="선택")
        assert r["결과"]["판정"] == "위반"
