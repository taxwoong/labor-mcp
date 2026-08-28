# -*- coding: utf-8 -*-
"""
calculators.py — 노무 수치 계산 엔진 (순수 함수, 네트워크 없음)

MCP 계산 도구 7종의 본체이며, 임금대장 분석기(payroll.py)가 행 단위로 재사용한다.

임금항목(item)은 임금대장 정규화 스키마의 "임금항목" dict를 그대로 받는다:
  {"명칭": str, "금액": int, "지급주기": "매월|격월|분기|반기|연간|일회성",
   "성격": "기본급|정기상여|식대|교통비|직책수당|가족수당|기술수당|성과급|
            연장수당|야간수당|휴일수당|연차수당|기타",
   "실비변상": bool(기본 False), "최소보장액": int(성과급일 때만, 선택)}

공통 반환 규약: {"결과": {...}, "계산과정": [str], "근거": [str], "주의사항": [str]}
(classify_wage_item만 예외 — 3축 판정 dict를 그대로 반환)

연도 의존 수치·가산율·연차 규칙은 전부 labor_constants에서 가져온다 (하드코딩 금지).
"""
import calendar
from datetime import date

import labor_constants as lc

__all__ = [
    "classify_wage_item",
    "calc_ordinary_wage",
    "calc_average_wage",
    "check_minimum_wage",
    "calc_weekly_holiday_pay",
    "calc_annual_leave",
    "calc_overtime_pay",
    "calc_severance_pay",
    "calc_dismissal_notice_pay",
    "calc_wage_cut_limit",
    "weekly_52h_check",
]

# 지급주기 → 월 환산 제수 (통상임금 월액 환산용). 일회성은 정기성이 없어 환산 대상 아님.
_PERIOD_MONTHS = {"매월": 1, "격월": 2, "분기": 3, "반기": 6, "연간": 12}

# 소정근로 외 근로의 대가 — 최저임금 불산입(최저임금법 §6④1호)·통상임금 제외
_NON_CONTRACTUAL_KINDS = frozenset({"연장수당", "야간수당", "휴일수당", "연차수당"})

# 생활보조·복리후생 성질의 통화 지급 임금 — 2019~2023 최저임금 부분산입 대상
# (최저임금법 §6④3호나목. 2024년부터 매월 지급분 전액 산입)
_WELFARE_KINDS = frozenset({"식대", "교통비", "가족수당"})


def _fmt(x) -> str:
    """계산과정 표시용 금액 포맷."""
    return f"{x:,.1f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"


def _norm_date(s: str) -> str:
    """'YYYY-MM-DD' 또는 'YYYYMMDD' → 'YYYYMMDD'."""
    return s.replace("-", "")


# ---------------------------------------------------------------------------
# 1. 임금항목 3축 판정 (최저임금 산입 × 통상임금 포함 × 평균임금 포함)
# ---------------------------------------------------------------------------

def classify_wage_item(item: dict, year: int) -> dict:
    """임금항목 1건의 3축 판정.

    반환: {"최저임금산입": "산입|부분산입(사유)|불산입(사유)",
           "통상임금": "포함|포함(최소보장액 한정)|제외(사유)|불확실(사유)",
           "평균임금": "포함|제외(사유)",
           "통상임금산입액": int,   # 통상임금에 산입되는 금액(지급주기 단위 그대로 —
                                    # 월 환산은 calc_ordinary_wage가 수행)
           "근거": [str]}

    판정 규칙(2026-08-28 리서치 확정):
    - 최저임금: 매월 1회 이상 정기 지급분만 산입. 2019~2023년은 정기상여·현금성
      복리후생비에 연도별 미산입 비율 적용(부분산입 — 임계액 계산은 check_minimum_wage
      가 그룹 합계로 수행). 실비변상·소정근로 외 임금 불산입.
    - 통상임금: 2024-12-19 전원합의체(2020다247190·2023다302838) 신법리 —
      '고정성' 폐기, 정기성·일률성만 판단. 재직조건·최소근무일수 조건 무관.
      성과급은 최소보장액만, 가족수당은 차등 지급 여부 불명이므로 불확실.
    - 평균임금: 임금성(근로의 대가) 있으면 포함 — 실비변상만 제외.
    """
    금액 = int(item.get("금액", 0))
    주기 = item.get("지급주기", "매월")
    성격 = item.get("성격", "기타")
    실비 = bool(item.get("실비변상", False))
    최소보장 = int(item.get("최소보장액") or 0)
    근거 = []

    # --- 축 1: 최저임금 산입 ---
    if 실비:
        mw = "불산입(실비변상 — 임금이 아님)"
        근거.append("최저임금법 시행규칙 §2 — 실비변상적 금품은 최저임금 불산입")
    elif 성격 in _NON_CONTRACTUAL_KINDS:
        mw = "불산입(소정근로 외 임금)"
        근거.append("최저임금법 §6④1호 — 소정근로시간 외 근로에 대한 임금 불산입")
    elif 주기 == "일회성":
        mw = "불산입(일시 지급 — 매월 1회 이상 정기 지급 아님)"
        근거.append("최저임금법 §6④ — 매월 1회 이상 정기 지급 임금만 산입")
    elif 주기 != "매월":
        mw = f"불산입({주기} 지급 — 매월 1회 이상 정기 지급 아님)"
        근거.append("최저임금법 §6④ — 격월·분기·연간 지급분은 최저임금 불산입")
    elif 성격 == "성과급":
        mw = "불산입(변동 성과급 — 소정근로의 대가 아님)"
        근거.append("최저임금법 §6④ — 소정근로 대가가 아닌 변동 성과급 불산입")
    elif 성격 == "정기상여":
        if year <= 2018:
            mw = "불산입(2018년 이전 구법 — 상여금 전면 불산입)"
            근거.append("구 최저임금법 §6④ (2018 개정 전) — 상여금 불산입")
        else:
            bonus_ratio, _ = lc.mw_exclusion_ratios(year)
            if bonus_ratio == 0:
                mw = "산입"
                근거.append("최저임금법 §6④2호 — 2024년부터 매월 지급 정기상여 전액 산입")
            else:
                mw = (f"부분산입(정기상여 — {year}년 월 환산 최저임금액의 "
                      f"{bonus_ratio:.0%} 초과분만 산입)")
                근거.append("최저임금법 부칙(법률 제15666호) — 정기상여 미산입 비율 연도별 축소")
    elif 성격 in _WELFARE_KINDS:
        if year <= 2018:
            mw = "불산입(2018년 이전 구법 — 복리후생비 전면 불산입)"
            근거.append("구 최저임금법 §6④ (2018 개정 전) — 복리후생비 불산입")
        else:
            _, welfare_ratio = lc.mw_exclusion_ratios(year)
            if welfare_ratio == 0:
                mw = "산입"
                근거.append("최저임금법 §6④3호나목 — 2024년부터 매월 지급 현금성 복리후생비 전액 산입")
            else:
                mw = (f"부분산입(복리후생 — {year}년 월 환산 최저임금액의 "
                      f"{welfare_ratio:.0%} 초과분만 산입)")
                근거.append("최저임금법 부칙(법률 제15666호) — 복리후생비 미산입 비율 연도별 축소")
    else:
        mw = "산입"
        근거.append("최저임금법 §6④ — 매월 1회 이상 정기 지급하는 소정근로 대가 산입")

    # --- 축 2: 통상임금 (2024-12-19 전합 신법리) ---
    ow_amount = 0
    if 실비:
        ow = "제외(실비변상 — 임금성 없음)"
        근거.append("실비변상 금품은 근로의 대가가 아니어서 통상임금 제외")
    elif 성격 in _NON_CONTRACTUAL_KINDS:
        ow = "제외(소정근로의 대가 아님 — 연장·야간·휴일·연차수당류)"
        근거.append("통상임금은 소정근로의 대가 — 법정수당류는 산정 기초에서 제외")
    elif 주기 == "일회성":
        ow = "제외(일시 지급 — 정기성 없음)"
        근거.append("일시적·비정기 지급분은 정기성 결여로 통상임금 제외")
    elif 성격 == "성과급":
        if 최소보장 > 0:
            ow = "포함(최소보장액 한정)"
            ow_amount = 최소보장
            근거.append("성과급 중 최소보장액은 고정 지급분으로서 통상임금 포함 "
                        "(대법원 전원합의체 2024-12-19 선고 2020다247190·2023다302838)")
        else:
            ow = "제외(변동 성과급 — 소정근로 대가인 고정분 없음)"
            근거.append("실적에 따라 변동하는 성과급은 소정근로의 대가가 아니어서 통상임금 제외")
    elif 성격 == "가족수당":
        ow = ("불확실(부양가족 수에 따라 차등 지급이면 일률성 결여로 제외 — "
              "전 직원 일률 지급분만 포함)")
        근거.append("가족수당은 지급 방식에 따라 일률성 판단이 갈림 — 개별 확인 필요")
    elif 성격 == "기타":
        ow = "불확실(항목 성격 미상 — 소정근로 대가성 개별 판단 필요)"
        근거.append("성격 미상 항목은 소정근로 대가성·정기성·일률성 개별 검토 필요")
    else:
        # 기본급·정기상여(주기 무관)·식대·교통비·직책수당·기술수당 등 정기 고정 지급분
        ow = "포함"
        ow_amount = 금액
        근거.append("대법원 전원합의체 2024-12-19 선고 2020다247190·2023다302838 — "
                    "'고정성' 폐기: 재직조건·최소근무일수 조건 무관, 정기성·일률성 충족 시 통상임금")

    # --- 축 3: 평균임금 ---
    if 실비:
        aw = "제외(실비변상 — 임금성 없음)"
        근거.append("근로기준법 §2①6호 — 평균임금은 '임금'의 총액 기준, 실비변상 제외")
    else:
        aw = "포함"
        근거.append("근로기준법 §2①6호 — 근로의 대가로 지급된 임금은 평균임금 산정에 포함")

    return {"최저임금산입": mw, "통상임금": ow, "평균임금": aw,
            "통상임금산입액": ow_amount, "근거": 근거}


# ---------------------------------------------------------------------------
# 2. 통상임금 (월액·시급)
# ---------------------------------------------------------------------------

def calc_ordinary_wage(items: list, weekly_hours: float, year: int,
                       as_of_date: str = None) -> dict:
    """월 통상임금과 통상시급.

    items: 임금항목 dict 리스트 (한 달치 임금대장 항목 — 격월·분기·연간 항목은
           그 주기의 1회 지급액을 그대로 넣으면 월 환산해 산입한다).
    as_of_date: 통상임금 '산정 시점'(YYYY-MM-DD). 2024-12-19 전합 판결은 장래효 —
           그 이전 산정분은 구법리(고정성 요건)가 적용되므로 주의 문구를 붙인다.
    """
    steps, 근거, 주의 = [], [], []
    monthly_total = 0.0
    uncertain = []

    for item in items:
        명칭 = item.get("명칭", "(무명)")
        cls = classify_wage_item(item, year)
        판정 = cls["통상임금"]
        if 판정.startswith("포함"):
            months = _PERIOD_MONTHS.get(item.get("지급주기", "매월"))
            if months is None:
                continue
            monthly = cls["통상임금산입액"] / months
            monthly_total += monthly
            if months == 1:
                steps.append(f"{명칭}: {_fmt(cls['통상임금산입액'])}원 포함")
            else:
                steps.append(f"{명칭}: {_fmt(cls['통상임금산입액'])}원 ÷ {months}개월 "
                             f"= 월 {_fmt(monthly)}원 포함")
        elif 판정.startswith("불확실"):
            uncertain.append(명칭)
            steps.append(f"{명칭}: 불확실 — 산정에서 제외 ({판정})")
            주의.append(f"'{명칭}' 항목은 통상임금 포함 여부가 불확실해 제외하고 계산 — {판정}")
        else:
            steps.append(f"{명칭}: 제외 ({판정})")
        근거.extend(g for g in cls["근거"] if g not in 근거)

    std_hours = lc.monthly_standard_hours(weekly_hours)
    hourly = monthly_total / std_hours if std_hours else 0.0
    steps.append(f"월 통상임금 합계 {_fmt(monthly_total)}원")
    steps.append(f"월 소정근로시간(주휴 포함) = {std_hours}시간 (주 {weekly_hours}시간 기준)")
    steps.append(f"통상시급 = {_fmt(monthly_total)} ÷ {std_hours} = {hourly:,.2f}원")

    근거.append("근로기준법 시행령 §6 — 통상임금의 시간급 환산")
    주의.append("2024-12-19 전원합의체 신법리(고정성 폐기) 기준 판정 — "
               "재직조건·최소근무일수 조건이 있어도 정기상여는 통상임금에 포함된다.")
    if as_of_date and _norm_date(as_of_date) < lc.ORDINARY_WAGE_RULING_DATE:
        주의.append(f"산정 시점({as_of_date})이 2024-12-19 이전 — 전합 판결은 장래효이므로 "
                   "그 시점 산정분에는 구법리(고정성 요건)가 적용되어 재직조건부 상여 등이 "
                   "제외될 수 있다. 본 결과는 신법리 기준.")

    return {
        "결과": {
            "월통상임금": round(monthly_total, 2),
            "통상시급": round(hourly, 2),
            "월기준시간": std_hours,
            "불확실항목": uncertain,
        },
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }


# ---------------------------------------------------------------------------
# 3. 평균임금 (1일)
# ---------------------------------------------------------------------------

def calc_average_wage(last_3m_items: list, annual_bonus_total: int,
                      annual_leave_pay_total: int, days_in_window: int,
                      ordinary_wage_monthly: float = None,
                      weekly_hours: float = 40.0) -> dict:
    """1일 평균임금 (근기법 §2①6호 — 사유 발생일 이전 3개월 임금총액 ÷ 총일수).

    last_3m_items: 월별 임금항목 리스트의 리스트 (3개월분). 창구 합산에는
        '매월 지급' 항목만 넣는다 — 격월·분기·연간 정기상여와 연차수당은
        annual_bonus_total(연간 상여 총액)·annual_leave_pay_total(연간 연차수당 총액)로
        전달하면 3/12만 산입한다(행정해석·판례 확립 산식). 창구 안의 비매월 항목·
        연차수당은 이중 산입 방지를 위해 자동 제외된다.
    days_in_window: 3개월의 역일수 (89~92).
    ordinary_wage_monthly: 월 통상임금 — 주면 통상임금 하한(§2②)과 비교한다.
    weekly_hours: 하한 비교 시 1일 통상임금 환산용 주 소정근로시간 (주 5일제 가정).
    """
    steps, 근거, 주의 = [], [], []
    window_total = 0
    skipped = []
    for month_items in last_3m_items:
        for item in month_items:
            명칭 = item.get("명칭", "(무명)")
            if bool(item.get("실비변상", False)):
                skipped.append(f"{명칭}(실비변상)")
                continue
            if item.get("성격") == "연차수당":
                skipped.append(f"{명칭}(연차수당 — 연간 3/12 채널로 산입)")
                continue
            if item.get("지급주기", "매월") != "매월":
                skipped.append(f"{명칭}({item.get('지급주기')} 지급 — 연간 총액 3/12 채널로 산입)")
                continue
            window_total += int(item.get("금액", 0))

    bonus_in = annual_bonus_total * lc.ANNUAL_ITEM_INCLUSION
    leave_in = annual_leave_pay_total * lc.ANNUAL_ITEM_INCLUSION
    total = window_total + bonus_in + leave_in
    daily = total / days_in_window if days_in_window else 0.0

    steps.append(f"3개월 임금총액(매월 지급분) = {_fmt(window_total)}원")
    if annual_bonus_total:
        steps.append(f"연간 정기상여 {_fmt(annual_bonus_total)}원 × 3/12 = {_fmt(bonus_in)}원 산입")
    if annual_leave_pay_total:
        steps.append(f"연간 연차수당 {_fmt(annual_leave_pay_total)}원 × 3/12 = {_fmt(leave_in)}원 산입")
    steps.append(f"산정 임금총액 = {_fmt(total)}원, 총일수 = {days_in_window}일")
    steps.append(f"1일 평균임금 = {_fmt(total)} ÷ {days_in_window} = {daily:,.2f}원")
    if skipped:
        주의.append("창구 합산 제외 항목: " + ", ".join(skipped))

    근거.append("근로기준법 §2①6호 — 평균임금 = 산정사유 발생일 이전 3개월 임금총액 ÷ 총일수")
    근거.append("연간 단위 정기상여·연차수당은 연간 총액의 3/12 산입 (고용노동부 행정해석·판례 확립)")

    ow_daily = None
    applied = daily
    floor_applied = False
    if ordinary_wage_monthly is not None:
        std_hours = lc.monthly_standard_hours(weekly_hours)
        hourly = ordinary_wage_monthly / std_hours
        daily_contract_hours = min(weekly_hours, lc.STATUTORY_WEEKLY_HOURS) / 5
        ow_daily = hourly * daily_contract_hours
        steps.append(f"1일 통상임금 = ({_fmt(ordinary_wage_monthly)} ÷ {std_hours}시간) × "
                     f"{daily_contract_hours}시간 = {ow_daily:,.2f}원")
        if daily < ow_daily:
            applied = ow_daily
            floor_applied = True
            steps.append("평균임금이 통상임금보다 낮음 → 통상임금을 평균임금으로 적용 (§2②)")
        근거.append("근로기준법 §2② — 평균임금이 통상임금보다 적으면 통상임금을 평균임금으로 한다")
        주의.append("1일 통상임금은 주 5일제(1일 소정 = 주소정÷5) 가정으로 환산 — "
                   "근로일 구성이 다르면 1일 소정근로시간으로 재환산 필요.")

    주의.append("휴업·휴직 등으로 평균임금이 현저히 낮아진 기간은 산정에서 제외해야 한다 "
               "(시행령 §2 — 본 함수는 입력된 창구를 그대로 사용).")

    return {
        "결과": {
            "1일평균임금": round(daily, 2),
            "적용평균임금": round(applied, 2),
            "통상임금하한적용": floor_applied,
            "1일통상임금": round(ow_daily, 2) if ow_daily is not None else None,
            "임금총액": round(total, 2),
            "상여산입액": round(bonus_in, 2),
            "연차수당산입액": round(leave_in, 2),
        },
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }


# ---------------------------------------------------------------------------
# 4. 최저임금 검증
# ---------------------------------------------------------------------------

def check_minimum_wage(items: list, weekly_hours: float, year: int,
                       probation: bool = False, contract_1yr_plus: bool = False,
                       simple_labor: bool = False) -> dict:
    """최저임금 위반 검증 — 산입범위 분해 → 비교시급 산출 → 수습 감액 반영 → 부족액.

    미등록 연도는 labor_constants.minimum_wage()가 ValueError를 던진다 (조용히
    최신값으로 대체하지 않음 — 갱신 강제).
    """
    mw_hourly = lc.minimum_wage(year)
    std_hours = lc.monthly_standard_hours(weekly_hours)
    mw_monthly = mw_hourly * std_hours
    steps, 근거, 주의 = [], [], []

    # 수습 감액 — 3요건(1년 이상 계약 + 3개월 이내 + 단순노무직 아님) 모두 충족 시만 10%
    reduction = False
    applied_hourly = float(mw_hourly)
    if probation:
        if contract_1yr_plus and not simple_labor:
            reduction = True
            applied_hourly = mw_hourly * (1 - lc.PROBATION_REDUCTION_RATE)
            steps.append(f"수습 감액 적용: {_fmt(mw_hourly)} × "
                         f"{1 - lc.PROBATION_REDUCTION_RATE:.0%} = {applied_hourly:,.1f}원")
            근거.append("최저임금법 §5② + 시행령 §3 — 1년 이상 계약 + 수습 3개월 이내 + "
                       "단순노무직 아님 → 10% 감액 가능")
            주의.append("수습 감액은 수습 시작일부터 3개월 이내 기간에만 유효 — 기간 경과 여부 확인 필요.")
        else:
            사유 = []
            if not contract_1yr_plus:
                사유.append("근로계약 기간 1년 미만")
            if simple_labor:
                사유.append("단순노무직(한국표준직업분류 대분류9)")
            주의.append("수습이지만 감액 불가(" + ", ".join(사유) + ") — 최저임금 전액 적용.")
            근거.append("최저임금법 §5②·시행령 §3 — 감액 3요건 미충족 시 감액 불가")

    # 산입범위 분해 — 부분산입(2019~2023)은 항목별이 아니라 그룹 합계에 임계액을 적용
    full_sum = 0
    bonus_sum = 0        # 매월 지급 정기상여 (부분산입 그룹)
    welfare_sum = 0      # 매월 지급 현금성 복리후생비 (부분산입 그룹)
    detail = []
    for item in items:
        명칭 = item.get("명칭", "(무명)")
        금액 = int(item.get("금액", 0))
        cls = classify_wage_item(item, year)
        판정 = cls["최저임금산입"]
        detail.append({"명칭": 명칭, "금액": 금액, "판정": 판정})
        if 판정 == "산입":
            full_sum += 금액
        elif 판정.startswith("부분산입"):
            if item.get("성격") == "정기상여":
                bonus_sum += 금액
            else:
                welfare_sum += 금액
        else:
            steps.append(f"{명칭} {_fmt(금액)}원: {판정}")
        근거.extend(g for g in cls["근거"] if g not in 근거)

    bonus_in = welfare_in = 0.0
    if bonus_sum or welfare_sum:
        bonus_ratio, welfare_ratio = lc.mw_exclusion_ratios(year)
        if bonus_sum:
            threshold = mw_monthly * bonus_ratio
            bonus_in = max(0.0, bonus_sum - threshold)
            steps.append(f"정기상여 {_fmt(bonus_sum)}원 중 월 환산 최저임금 "
                         f"{_fmt(mw_monthly)}원의 {bonus_ratio:.0%}({_fmt(threshold)}원) "
                         f"초과분 {_fmt(bonus_in)}원 산입")
        if welfare_sum:
            threshold = mw_monthly * welfare_ratio
            welfare_in = max(0.0, welfare_sum - threshold)
            steps.append(f"복리후생비 {_fmt(welfare_sum)}원 중 월 환산 최저임금의 "
                         f"{welfare_ratio:.0%}({_fmt(threshold)}원) 초과분 "
                         f"{_fmt(welfare_in)}원 산입")

    total_in = full_sum + bonus_in + welfare_in
    compare_hourly = total_in / std_hours if std_hours else 0.0
    steps.append(f"전액 산입분 {_fmt(full_sum)}원 → 산입액 합계 {_fmt(total_in)}원")
    steps.append(f"비교시급 = {_fmt(total_in)} ÷ {std_hours}시간 = {compare_hourly:,.2f}원")
    steps.append(f"적용 최저시급 {applied_hourly:,.1f}원과 비교 "
                 f"(고시 최저시급 {_fmt(mw_hourly)}원, 월 환산 {_fmt(mw_monthly)}원)")

    violation = compare_hourly < applied_hourly - 1e-9
    shortfall = round(applied_hourly * std_hours - total_in, 2) if violation else 0
    if violation:
        steps.append(f"위반 — 월 부족액 = {applied_hourly:,.1f} × {std_hours} − "
                     f"{_fmt(total_in)} = {_fmt(shortfall)}원")
        주의.append("최저임금 미달 부분은 무효이며 최저임금액과 동일한 임금 지급을 약정한 "
                   "것으로 본다 (최저임금법 §6③).")

    근거.append(f"최저임금 고시 — {year}년 시급 {_fmt(mw_hourly)}원")
    근거.append("최저임금법 §6 — 산입범위·비교 방법")

    return {
        "결과": {
            "연도": year,
            "최저시급": mw_hourly,
            "적용최저시급": round(applied_hourly, 2),
            "수습감액적용": reduction,
            "월환산최저임금": round(mw_monthly, 2),
            "월기준시간": std_hours,
            "전액산입합계": full_sum,
            "정기상여_산입액": round(bonus_in, 2),
            "복리후생_산입액": round(welfare_in, 2),
            "산입액합계": round(total_in, 2),
            "비교시급": round(compare_hourly, 2),
            "위반여부": violation,
            "월부족액": shortfall,
            "산입내역": detail,
        },
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }


# ---------------------------------------------------------------------------
# 5. 주휴수당
# ---------------------------------------------------------------------------

def calc_weekly_holiday_pay(weekly_hours: float, hourly_wage: float,
                            perfect_attendance: bool = True) -> dict:
    """1주 주휴수당. 주 소정 15시간 미만 제외(§18③), 개근 요건(§55①·시행령 §30①).
    단시간근로자는 (주소정/40)×8시간으로 비례. 5인 미만 사업장에도 적용."""
    steps, 근거, 주의 = [], [], []
    근거.append("근로기준법 §55① — 1주 개근 시 유급휴일(주휴) 보장")

    if weekly_hours < lc.WEEKLY_HOLIDAY_MIN_HOURS:
        steps.append(f"주 소정근로 {weekly_hours}시간 < {lc.WEEKLY_HOLIDAY_MIN_HOURS}시간 → 주휴 미발생")
        근거.append("근로기준법 §18③ — 4주 평균 1주 15시간 미만 근로자는 주휴·연차 적용 제외")
        return {"결과": {"주휴시간": 0.0, "주휴수당": 0},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    if not perfect_attendance:
        steps.append("해당 주 개근 아님 → 주휴수당 미발생 (주휴일 자체는 무급 부여)")
        근거.append("근로기준법 시행령 §30① — 주휴는 1주 소정근로일 개근 요건")
        return {"결과": {"주휴시간": 0.0, "주휴수당": 0},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    holiday_hours = min(weekly_hours, lc.STATUTORY_WEEKLY_HOURS) / lc.STATUTORY_WEEKLY_HOURS * 8
    pay = holiday_hours * hourly_wage
    steps.append(f"주휴시간 = ({min(weekly_hours, lc.STATUTORY_WEEKLY_HOURS)} ÷ "
                 f"{lc.STATUTORY_WEEKLY_HOURS}) × 8 = {holiday_hours}시간")
    steps.append(f"주휴수당 = {holiday_hours} × {_fmt(hourly_wage)} = {pay:,.2f}원")
    주의.append("'다음 주 근로 예정' 요건은 폐지됨(임금근로시간과-1736, 2021-08-04) — "
               "마지막 주라도 개근했으면 주휴 발생. 주휴수당은 5인 미만 사업장에도 적용.")

    return {"결과": {"주휴시간": round(holiday_hours, 2), "주휴수당": round(pay, 2)},
            "계산과정": steps, "근거": 근거, "주의사항": 주의}


# ---------------------------------------------------------------------------
# 6. 연차유급휴가
# ---------------------------------------------------------------------------

def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def calc_annual_leave(hire_date: str, base_date: str, attendance_rate: float = 1.0,
                      mode: str = "입사일", fiscal_start: str = "01-01",
                      employed_on_base_date: bool = True) -> dict:
    """base_date까지 발생한 연차유급휴가 일수 (근기법 §60).

    - hire_date/base_date: "YYYY-MM-DD". base_date는 산정 기준일 — 퇴직 정산이면
      '마지막 근로일의 다음 날'(관행상 퇴직일)을 넣고 employed_on_base_date=False.
    - 연차는 요건 충족 기간의 '다음 날' 근로관계가 존속해야 발생 (고용노동부 행정해석
      2021-12-16 변경) — 발생일이 base_date 당일인 연차는 employed_on_base_date=True
      (그 날 재직)일 때만 인정한다.
    - attendance_rate: 직전 연차산정 연도의 출근율. 0.8 미만이면 그 연도 연차 미발생
      (§60② — 개근 월당 1일은 월별 출근 데이터가 필요해 여기서는 0으로 처리).
      과거 연도는 80% 이상 출근 가정.
    - mode="회계연도": fiscal_start("MM-DD") 기준 부여 방식으로도 계산해 입사일 기준과
      비교한다. 1년 미만 월 개근분은 두 방식 모두 입사일 기준(법정)이다.
    """
    if mode not in ("입사일", "회계연도"):
        raise ValueError(f"mode는 '입사일' 또는 '회계연도' — 받은 값: {mode!r}")
    hire = date.fromisoformat(hire_date)
    base = date.fromisoformat(base_date)
    if base < hire:
        raise ValueError(f"base_date({base_date})가 hire_date({hire_date})보다 이전입니다.")

    def accrues(ev: date) -> bool:
        return ev < base or (ev == base and employed_on_base_date)

    steps, 근거, 주의 = [], [], []
    detail = []

    # --- 1년 미만 월 개근분 (최대 11일) — 발생일 = 개근한 달의 다음 날 ---
    monthly_days = 0
    for k in range(1, lc.ANNUAL_LEAVE_FIRST_YEAR_MAX + 1):
        ev = _add_months(hire, k)
        if accrues(ev):
            monthly_days += 1
            detail.append({"발생일": ev.isoformat(), "구분": "1년미만 월개근", "일수": 1})
    if monthly_days:
        steps.append(f"1년 미만 월 개근분: {monthly_days}일 (월 1일, 최대 "
                     f"{lc.ANNUAL_LEAVE_FIRST_YEAR_MAX}일 — §60②)")
    if attendance_rate < 1.0 and monthly_days:
        주의.append("1년 미만 월 개근분은 '개근한 달'만 발생 — 월별 출근 데이터가 없어 "
                   "전월 개근 가정으로 계산했다.")

    # --- 입사일 기준 연차 (발생일 = 입사 후 n년이 되는 날 = 1년 근로를 마친 다음 날) ---
    ann_events = []
    n = 1
    while True:
        ev = _add_months(hire, 12 * n)
        if ev > base:
            break
        if accrues(ev):
            ann_events.append((n, ev))
        n += 1
    ann_total = 0
    for i, (yr, ev) in enumerate(ann_events):
        days = lc.annual_leave_days(yr)
        if i == len(ann_events) - 1 and attendance_rate < 0.8:
            days = 0
            steps.append(f"{ev.isoformat()}: 출근율 {attendance_rate:.0%} < 80% → "
                         f"{yr}년차 연차 미발생 (§60①·②)")
        else:
            steps.append(f"{ev.isoformat()}: {yr}년 근속 연차 {days}일 발생 (가산 포함 — §60①·④)")
        ann_total += days
        detail.append({"발생일": ev.isoformat(), "구분": "연차(입사일)", "일수": days})
    hire_based_total = monthly_days + ann_total

    # --- 회계연도 기준 (mode="회계연도"일 때만) ---
    fiscal_total = None
    fiscal_detail = []
    if mode == "회계연도":
        fm, fd = (int(x) for x in fiscal_start.split("-"))
        fiscal_dates = []
        for y in range(hire.year, base.year + 1):
            f = date(y, fm, min(fd, calendar.monthrange(y, fm)[1]))
            if hire < f <= base and accrues(f):
                fiscal_dates.append(f)
        fiscal_sum = 0.0
        for i, f in enumerate(fiscal_dates):
            if i == 0:
                days = round(lc.ANNUAL_LEAVE_BASE * (f - hire).days / 365, 1)
                구분 = "비례연차(회계연도)"
                steps.append(f"{f.isoformat()}: 비례연차 {lc.ANNUAL_LEAVE_BASE} × "
                             f"{(f - hire).days}/365 = {days}일 (회계연도 방식)")
            else:
                days = lc.annual_leave_days(i)
                구분 = "연차(회계연도)"
                steps.append(f"{f.isoformat()}: 회계연도 연차 {days}일")
            if i == len(fiscal_dates) - 1 and attendance_rate < 0.8:
                steps.append(f"{f.isoformat()}: 출근율 {attendance_rate:.0%} < 80% → 미발생")
                days = 0
            fiscal_sum += days
            fiscal_detail.append({"발생일": f.isoformat(), "구분": 구분, "일수": days})
        fiscal_total = round(monthly_days + fiscal_sum, 2)
        주의.append("회계연도 방식은 법정 방식(입사일)이 아닌 편의 부여 — 퇴직 시 입사일 기준 "
                   "발생분에 미달하면 차액을 정산해야 한다. 가산 계산 방식은 회사 규정에 따라 "
                   "다를 수 있다.")

    if mode == "입사일":
        total = round(hire_based_total, 2)
        연차분 = ann_total
        결과내역 = detail
        guaranteed = total
    else:
        total = fiscal_total
        연차분 = round(fiscal_total - monthly_days, 2)
        결과내역 = [d for d in detail if d["구분"] == "1년미만 월개근"] + fiscal_detail
        guaranteed = max(round(hire_based_total, 2), fiscal_total)

    근거.append("근로기준법 §60① — 1년 80% 이상 출근 시 15일")
    근거.append("근로기준법 §60② — 1년 미만·80% 미만자는 1개월 개근 시 1일")
    근거.append("근로기준법 §60④ — 3년 이상 근속 시 2년마다 1일 가산, 총 25일 한도")
    근거.append("대법원 2021-10-14 선고 2021다227100 — 1년 기간제 연차는 최대 11일 "
               "(1년 근로 종료 다음 날 재직하지 않으면 15일분 미발생)")
    근거.append("고용노동부 행정해석 변경(2021-12-16) — 연차는 요건 충족 기간의 다음 날 "
               "근로관계 존속 시 발생")
    주의.append("상시 5인 미만 사업장에는 연차유급휴가 규정(§60)이 적용되지 않는다 "
               "(근기법 §11·시행령 별표1).")
    주의.append("2026-02-19 개정으로 임신기·육아기 근로시간 단축 기간은 출근으로 간주된다 "
               "(§60⑥4·5호) — 출근율 산정 시 반영할 것.")

    return {
        "결과": {
            "총발생일수": total,
            "월개근분": monthly_days,
            "연차분": 연차분,
            "입사일기준총일수": round(hire_based_total, 2),
            "회계연도기준총일수": fiscal_total,
            "보장기준일수": guaranteed,
            "발생내역": 결과내역,
        },
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }


# ---------------------------------------------------------------------------
# 7. 연장·야간·휴일 가산수당
# ---------------------------------------------------------------------------

def calc_overtime_pay(ordinary_hourly: float, overtime_h: float, night_h: float,
                      holiday_h: float, holiday_over8_h: float = 0,
                      employees: int = 5) -> dict:
    """연장·야간·휴일근로 수당 (근기법 §56). 중복 가산 허용.

    - 연장·휴일 수당은 근로 시간 자체의 임금(100%)을 포함한 금액, 야간은 가산분(50%)만
      — 야간근로는 통상 소정근로·연장근로와 겹쳐 기본분이 이미 지급되기 때문.
    - holiday_h가 8시간을 넘고 holiday_over8_h가 0이면 8시간 초과분을 자동 분리한다.
    - 상시 5인 미만(employees<5) 사업장은 §56 미적용 — 가산 없이 근로 시간분(100%)만.
    """
    steps, 근거, 주의 = [], [], []

    if holiday_over8_h == 0 and holiday_h > 8:
        holiday_over8_h = holiday_h - 8
        holiday_h = 8.0
        steps.append(f"휴일근로 {holiday_h + holiday_over8_h}시간 중 8시간 초과분 "
                     f"{holiday_over8_h}시간 자동 분리")

    if employees < lc.FULL_APPLICATION_MIN_EMPLOYEES:
        # 가산 미적용 — 단 근로 제공 시간 자체의 임금(100%)은 당연히 지급 의무
        ot = ordinary_hourly * overtime_h
        night = 0.0
        hol_in = ordinary_hourly * holiday_h
        hol_over = ordinary_hourly * holiday_over8_h
        steps.append(f"상시 {employees}인 사업장 — 근기법 §56 가산 미적용")
        if overtime_h:
            steps.append(f"연장 {overtime_h}시간 × {_fmt(ordinary_hourly)} × 1.0 = {_fmt(ot)}원 (가산 없음)")
        if night_h:
            steps.append(f"야간 {night_h}시간: 가산 없음 → 0원 (기본 임금분은 소정근로 급여에 포함)")
        if holiday_h or holiday_over8_h:
            steps.append(f"휴일 {holiday_h + holiday_over8_h}시간 × {_fmt(ordinary_hourly)} × 1.0 "
                         f"= {_fmt(hol_in + hol_over)}원 (가산 없음)")
        근거.append("근로기준법 §11·시행령 별표1 — 상시 4인 이하 사업장은 §56(연장·야간·휴일 "
                   "가산) 미적용")
        주의.append("5인 미만 사업장이라 가산수당은 발생하지 않지만, 실제 근로한 시간에 대한 "
                   "임금(100%)은 지급해야 한다. §50~53 근로시간 한도도 미적용.")
    else:
        ot = ordinary_hourly * overtime_h * (1 + lc.OVERTIME_PREMIUM)
        night = ordinary_hourly * night_h * lc.NIGHT_PREMIUM
        hol_in = ordinary_hourly * holiday_h * (1 + lc.HOLIDAY_PREMIUM_WITHIN_8H)
        hol_over = ordinary_hourly * holiday_over8_h * (1 + lc.HOLIDAY_PREMIUM_OVER_8H)
        if overtime_h:
            steps.append(f"연장수당 = {overtime_h}시간 × {_fmt(ordinary_hourly)} × "
                         f"{1 + lc.OVERTIME_PREMIUM} = {_fmt(ot)}원")
        if night_h:
            steps.append(f"야간가산 = {night_h}시간 × {_fmt(ordinary_hourly)} × "
                         f"{lc.NIGHT_PREMIUM} = {_fmt(night)}원 (연장·휴일과 중복 가산)")
        if holiday_h:
            steps.append(f"휴일수당(8h 이내) = {holiday_h}시간 × {_fmt(ordinary_hourly)} × "
                         f"{1 + lc.HOLIDAY_PREMIUM_WITHIN_8H} = {_fmt(hol_in)}원")
        if holiday_over8_h:
            steps.append(f"휴일수당(8h 초과) = {holiday_over8_h}시간 × {_fmt(ordinary_hourly)} × "
                         f"{1 + lc.HOLIDAY_PREMIUM_OVER_8H} = {_fmt(hol_over)}원")
        근거.append("근로기준법 §56① — 연장근로 50% 가산")
        근거.append("근로기준법 §56② — 휴일근로 8시간 이내 50%, 8시간 초과 100% 가산")
        근거.append("근로기준법 §56③ — 야간근로(22~06시) 50% 가산, 연장·휴일과 중복 적용")
        주의.append("포괄임금 약정이 있어도 실제 근로시간 기준 법정수당에 미달하면 그 차액을 "
                   "청구할 수 있다 (대법원 판례 확립).")

    total = ot + night + hol_in + hol_over
    steps.append(f"합계 = {_fmt(total)}원")

    return {
        "결과": {
            "연장수당": round(ot, 2),
            "야간가산수당": round(night, 2),
            "휴일수당_8시간이내": round(hol_in, 2),
            "휴일수당_8시간초과": round(hol_over, 2),
            "합계": round(total, 2),
            "가산적용": employees >= lc.FULL_APPLICATION_MIN_EMPLOYEES,
        },
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }


# ---------------------------------------------------------------------------
# 8. 퇴직급여
# ---------------------------------------------------------------------------

def calc_severance_pay(avg_daily_wage: float, service_days: int, weekly_hours: float,
                       plan_type: str = "퇴직금") -> dict:
    """법정 퇴직급여 (근로자퇴직급여 보장법).

    plan_type: "퇴직금" | "DB" — 평균임금 방식 (30일분 × 근속연수).
               "DC" — 산정식 자체가 다름: 연간 임금총액의 1/12 이상 부담금 납입 방식이라
               평균임금으로 계산할 수 없음을 안내하고 급여액은 None을 반환.
    """
    steps, 근거, 주의 = [], [], []
    근거.append("근로자퇴직급여 보장법 §4① — 계속근로 1년 이상, 4주 평균 주 15시간 이상")

    if weekly_hours < lc.WEEKLY_HOLIDAY_MIN_HOURS:
        steps.append(f"주 소정근로 {weekly_hours}시간 < 15시간 → 퇴직급여 설정 의무 제외")
        return {"결과": {"퇴직급여": 0, "제도": plan_type},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}
    if service_days < lc.SEVERANCE_MIN_SERVICE_DAYS:
        steps.append(f"계속근로 {service_days}일 < {lc.SEVERANCE_MIN_SERVICE_DAYS}일(1년) → 퇴직급여 미발생")
        주의.append("계속근로기간에는 수습·휴직 기간도 원칙적으로 포함 — 실제 기산일 확인 필요.")
        return {"결과": {"퇴직급여": 0, "제도": plan_type},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    if plan_type.upper().endswith("DC") or plan_type.upper() == "DC":
        steps.append("DC(확정기여)형 — 평균임금 방식 산정식 부적용")
        근거.append("근로자퇴직급여 보장법 §20① — DC형 부담금은 연간 임금총액의 1/12 이상")
        주의.append("DC형은 사용자가 매년 연간 임금총액의 1/12 이상을 부담금으로 납입하는 "
                   "방식 — 퇴직 시점 평균임금이 아니라 납입 누계+운용수익이 급여액이 된다. "
                   "미납·지연납입분은 지연이자와 함께 청구 가능.")
        return {"결과": {"퇴직급여": None, "제도": "DC",
                        "산정방식": "연간 임금총액의 1/12 이상 부담금 납입"},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    pay = avg_daily_wage * 30 * (service_days / 365)
    steps.append(f"퇴직금 = 1일 평균임금 {_fmt(avg_daily_wage)} × 30일 × "
                 f"({service_days}/365) = {pay:,.2f}원")
    근거.append("근로자퇴직급여 보장법 §8① — 계속근로 1년당 30일분 이상의 평균임금")
    주의.append("1일 평균임금이 통상임금보다 낮으면 통상임금으로 계산해야 한다 (근기법 §2②) — "
               "calc_average_wage의 하한 비교를 거친 값을 입력할 것.")

    return {"결과": {"퇴직급여": round(pay, 2), "제도": plan_type,
                    "근속연수환산": round(service_days / 365, 4)},
            "계산과정": steps, "근거": 근거, "주의사항": 주의}


# ---------------------------------------------------------------------------
# 9. 해고예고수당 · 감급 한도
# ---------------------------------------------------------------------------

def calc_dismissal_notice_pay(ordinary_daily: float, service_months: float,
                              exempt_reason: str = None) -> dict:
    """해고예고수당 (근기법 §26) — 30일 전 예고가 없으면 30일분 '이상'의 통상임금.

    exempt_reason: §26 단서 2·3호 사유(천재·사변으로 사업 계속 불가, 근로자의 고의로
    막대한 지장·손해 — 고용노동부령 사유)에 해당하면 그 문자열을 넣는다. 해당 여부의
    사실 판단은 이 함수 밖의 몫이며, 값이 있으면 예외로 처리한다.
    """
    steps, 근거, 주의 = [], [], []
    근거.append("근로기준법 §26 — 30일 전 예고 또는 30일분 이상의 통상임금(해고예고수당)")
    주의.append("해고예고를 지켰거나 수당을 지급했더라도 해고의 '정당한 이유'(근기법 §23①)가 "
               "없으면 부당해고다 — 해고예고 적법 ≠ 해고 정당성.")
    주의.append("해고예고 규정은 5인 미만 사업장에도 적용된다.")

    if service_months < lc.DISMISSAL_NOTICE_EXEMPT_MONTHS:
        steps.append(f"계속근로 {service_months}개월 < {lc.DISMISSAL_NOTICE_EXEMPT_MONTHS}개월 "
                     "→ 해고예고 적용 제외")
        근거.append("근로기준법 §26 단서 1호 — 계속근로 3개월 미만")
        return {"결과": {"해고예고수당": 0, "예외해당": True, "예외사유": "계속근로 3개월 미만"},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    if exempt_reason:
        steps.append(f"예외 사유 해당 → 해고예고수당 미발생: {exempt_reason}")
        근거.append("근로기준법 §26 단서 2·3호 — 천재·사변 등 부득이한 사유로 사업 계속 불가, "
                   "근로자의 귀책(고용노동부령 사유)")
        주의.append("§26 단서 2·3호 해당 여부는 엄격하게 해석된다 — 단순 경영난은 해당하지 않음.")
        return {"결과": {"해고예고수당": 0, "예외해당": True, "예외사유": exempt_reason},
                "계산과정": steps, "근거": 근거, "주의사항": 주의}

    pay = ordinary_daily * lc.DISMISSAL_NOTICE_DAYS
    steps.append(f"해고예고수당 = 1일 통상임금 {_fmt(ordinary_daily)} × "
                 f"{lc.DISMISSAL_NOTICE_DAYS}일 = {pay:,.2f}원")
    return {"결과": {"해고예고수당": round(pay, 2), "예외해당": False, "예외사유": None},
            "계산과정": steps, "근거": 근거, "주의사항": 주의}


def calc_wage_cut_limit(avg_daily_wage: float, wage_period_total: float) -> dict:
    """취업규칙에 따른 감급 제재의 한도 (근기법 §95).

    1회의 감급액은 평균임금 1일분의 1/2, 감급 총액은 1임금지급기 임금총액의 1/10을
    초과할 수 없다.
    """
    per_incident = avg_daily_wage * lc.WAGE_CUT_LIMIT_PER_INCIDENT
    per_period = wage_period_total * lc.WAGE_CUT_LIMIT_PER_PERIOD
    return {
        "결과": {"1회한도": round(per_incident, 2), "총액한도": round(per_period, 2)},
        "계산과정": [
            f"1회 감급 한도 = 평균임금 1일분 {_fmt(avg_daily_wage)} × 1/2 = {_fmt(per_incident)}원",
            f"1임금지급기 감급 총액 한도 = 임금총액 {_fmt(wage_period_total)} × 1/10 "
            f"= {_fmt(per_period)}원",
        ],
        "근거": ["근로기준법 §95 — 감급 제재의 제한"],
        "주의사항": ["한도를 넘는 감급 규정·처분은 그 초과 부분이 무효이며 임금체불이 된다."],
    }


# ---------------------------------------------------------------------------
# 10. 주 52시간 검사
# ---------------------------------------------------------------------------

def weekly_52h_check(weekly_hours_actual: list, weekly_statutory: float = 40,
                     flex_mode: str = "없음") -> dict:
    """주별 실근로시간의 연장근로 한도 검사 (근기법 §50·§53).

    weekly_hours_actual: 주별 실근로시간 리스트 (주차 순서대로).
    flex_mode: "없음" | "탄력" | "선택" — 탄력·선택근로제 사업장은 §53② 기준
        (단위기간·정산기간 '평균' 주 12시간)이라 주별 초과만으로 위반을 단정할 수 없다:
        평균으로도 초과하면 '위반', 특정 주만 초과하면 '불확실'(정산 단위 정보 필요).
    """
    limit = weekly_statutory + lc.MAX_WEEKLY_OVERTIME
    steps, 근거, 주의 = [], [], []
    detail = []
    over_weeks = []
    for i, h in enumerate(weekly_hours_actual, start=1):
        over = h > limit + 1e-9
        detail.append({"주차": i, "실근로시간": h, "한도초과": over,
                       "초과시간": round(max(0.0, h - limit), 2)})
        if over:
            over_weeks.append(i)
            steps.append(f"{i}주차 {h}시간 > 한도 {limit}시간 (초과 {round(h - limit, 2)}시간)")

    avg = (sum(weekly_hours_actual) / len(weekly_hours_actual)) if weekly_hours_actual else 0.0
    steps.append(f"검사 한도 = 소정 {weekly_statutory} + 연장 {lc.MAX_WEEKLY_OVERTIME} "
                 f"= 주 {limit}시간, 기간 평균 {avg:.2f}시간")
    근거.append("근로기준법 §50① — 1주 40시간, §53① — 합의 연장 주 12시간 한도")

    if flex_mode in ("탄력", "선택"):
        근거.append("근로기준법 §53② — 탄력(§51·51의2)·선택(§52)근로제에서도 연장은 "
                   "단위기간·정산기간 평균 주 12시간 이내")
        if avg > limit + 1e-9:
            verdict = "위반"
            steps.append(f"평균 {avg:.2f}시간 > {limit}시간 — 정산 단위와 무관하게 §53② 위반")
        elif over_weeks:
            verdict = "불확실"
            주의.append("탄력·선택근로제에서는 특정 주의 한도 초과만으로 위반을 단정할 수 없다 — "
                       "단위기간(정산기간) 전체의 평균 연장시간과 제도 요건(서면합의·대상기간 등) "
                       "확인 필요.")
        else:
            verdict = "적법"
    else:
        verdict = "위반" if over_weeks else "적법"
        if over_weeks:
            주의.append("주 52시간 초과는 근기법 §53① 위반 — §110 벌칙(2년 이하 징역 또는 "
                       "2천만원 이하 벌금) 대상. 30인 미만 특별연장(§53③)은 2022-12-31 "
                       "유효기간 만료로 더 이상 근거가 되지 않는다.")

    주의.append("상시 5인 미만 사업장에는 근로시간 한도(§50~53)가 적용되지 않는다.")

    return {
        "결과": {"판정": verdict, "위반주차": over_weeks, "주한도": limit,
                "평균주시간": round(avg, 2), "주별상세": detail},
        "계산과정": steps,
        "근거": 근거,
        "주의사항": 주의,
    }
