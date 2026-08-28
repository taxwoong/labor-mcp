# -*- coding: utf-8 -*-
"""
payroll.py — 임금대장 일괄 분석기(analyze_payroll) + 급여테이블 역산 설계기(design_pay_table)

calculators.py의 계산 엔진을 직원×월 행 단위로 재사용한다.
판정은 3단계(위반|적법|불확실) — 데이터 부족·법리 다툼 항목은 '불확실'로 분리하고
이유를 명시한다 (과잉 위반 판정 금지 — 개발계획.md §5).

입력 스키마(정규화 임금대장 — labor://schema/임금대장-입력 리소스와 동일):
{
  "사업장": {"상시근로자수": int, "연도": int, "탄력선택근로제": "없음|탄력|선택"},
  "직원": [
    {"사원ID": str, "성명": str(선택), "입사일": "YYYY-MM-DD", "퇴사일": "YYYY-MM-DD"|null,
     "주소정근로시간": float, "수습여부": bool, "계약기간1년이상": bool,
     "단순노무직": bool, "감시단속승인": bool,
     "월별": [{"연월": "YYYY-MM",
               "임금항목": [{"명칭","금액","지급주기","성격","실비변상","최소보장액"}],
               "근로일수": int|null, "총근로시간": float|null,
               "연장시간": float|null, "야간시간": float|null, "휴일시간": float|null,
               "주별근로시간": [float]|null, "공제내역": [{"항목","금액"}]|null}]}
  ]
}
null 필드는 "임금대장에 그 정보가 없음"을 뜻한다 — 기재사항 누락 검사(시행령 §27)와
연동하되, 4인 이하 사업장·§63 승인 근로자의 근로시간류(7·8호) 생략은 적법으로 본다.

analyze_payroll 출력:
{"직원별": [{"사원ID", "성명",
             "월별판정": [{"연월": "YYYY-MM"|None(직원 단위 검사),
                           "검사항목": "최저임금|통상임금|평균임금|주52시간|가산수당|기재사항|연차",
                           "판정": "위반|적법|불확실",
                           "상세": {"요지", "계산과정", "근거", "주의사항", ...검사별 수치}}]}],
 "요약": {"직원수", "총검사건수", "위반건수": {검사항목: 건수}, "위반합계",
          "불확실건수", "총부족액추정"},
 "경고": [str]}
"""
import calendar
from datetime import date, timedelta

import labor_constants as lc
import calculators as calc

__all__ = ["analyze_payroll", "design_pay_table"]

# 지급주기 → 월 환산 제수 (calculators.py와 동일 규약 — 일회성은 환산 대상 아님)
_PERIOD_MONTHS = {"매월": 1, "격월": 2, "분기": 3, "반기": 6, "연간": 12}

# 가산수당 실지급액으로 집계하는 임금항목 성격
_OT_PAY_KINDS = frozenset({"연장수당", "야간수당", "휴일수당"})

# 임금체불성 위반 → 상습 임금체불 제재(2025-10-23 시행) 경고 대상
_ARREARS_CHECKS = frozenset({"최저임금", "가산수당"})


def _fmt(x) -> str:
    return f"{x:,.1f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"


def _first_day(ym: str) -> date:
    return date(int(ym[:4]), int(ym[5:7]), 1)


def _last_day(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def _ym_add(ym: str, n: int) -> str:
    i = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + n
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _entry(ym, check, verdict, 요지, steps=None, basis=None, cautions=None, **extra) -> dict:
    상세 = {"요지": 요지, "계산과정": steps or [], "근거": basis or [],
           "주의사항": cautions or []}
    상세.update(extra)
    return {"연월": ym, "검사항목": check, "판정": verdict, "상세": 상세}


# ---------------------------------------------------------------------------
# 검사 ① 최저임금
# ---------------------------------------------------------------------------

def _check_min_wage(ym, year, items, weekly, probation_flag, c1yr, simple, hire_d):
    cautions = []
    eff = False
    if probation_flag and hire_d is not None:
        prob_end = _add_months(hire_d, 3)
        eff = _first_day(ym) < prob_end
        if eff and prob_end <= _last_day(ym):
            cautions.append(f"수습 3개월이 {prob_end.isoformat()}에 만료 — 이 달은 감액·정상 "
                            "구간이 섞여 월 단위 판정은 근사치.")
    try:
        r = calc.check_minimum_wage(items, weekly, year, probation=eff,
                                    contract_1yr_plus=c1yr, simple_labor=simple)
    except ValueError as e:
        # 미등록 연도 등 — 일괄 분석에서는 전체를 중단하지 않고 해당 월만 불확실 처리
        return _entry(ym, "최저임금", "불확실",
                      f"{year}년 최저임금 파라미터 미등록 — 판정 불가", cautions=[str(e)])
    res = r["결과"]
    violation = res["위반여부"]
    요지 = (f"비교시급 {_fmt(res['비교시급'])}원 vs 적용 최저시급 "
           f"{_fmt(res['적용최저시급'])}원 — {'미달' if violation else '충족'}")
    return _entry(ym, "최저임금", "위반" if violation else "적법", 요지,
                  steps=r["계산과정"], basis=r["근거"], cautions=r["주의사항"] + cautions,
                  부족액=res["월부족액"], 비교시급=res["비교시급"],
                  적용최저시급=res["적용최저시급"], 수습감액적용=res["수습감액적용"],
                  산입내역=res["산입내역"])


# ---------------------------------------------------------------------------
# 검사 ② 통상임금 산정
# ---------------------------------------------------------------------------

def _check_ordinary(ym, year, items, weekly):
    """통상임금 산정 entry와 (⑤가 재사용할) 통상시급을 함께 반환."""
    r = calc.calc_ordinary_wage(items, weekly, year, as_of_date=f"{ym}-01")
    res = r["결과"]
    항목판정 = [{"명칭": it.get("명칭", "(무명)"),
               "통상임금": (c := calc.classify_wage_item(it, year))["통상임금"],
               "최저임금산입": c["최저임금산입"], "평균임금": c["평균임금"]}
              for it in items]
    uncertain = res["불확실항목"]
    verdict = "불확실" if uncertain else "적법"
    요지 = f"월 통상임금 {_fmt(res['월통상임금'])}원, 통상시급 {_fmt(res['통상시급'])}원"
    if uncertain:
        요지 += f" — 불확실 항목({', '.join(uncertain)}) 제외 산정"
    entry = _entry(ym, "통상임금", verdict, 요지,
                   steps=r["계산과정"], basis=r["근거"], cautions=r["주의사항"],
                   월통상임금=res["월통상임금"], 통상시급=res["통상시급"],
                   월기준시간=res["월기준시간"], 불확실항목=uncertain, 항목판정=항목판정)
    return entry, res["통상시급"]


# ---------------------------------------------------------------------------
# 검사 ④ 주 52시간
# ---------------------------------------------------------------------------

def _check_52h(ym, mon, weekly, under5, guard, flex):
    if under5:
        return _entry(ym, "주52시간", "적법",
                      "상시 5인 미만 — 근로시간 한도(§50~53) 미적용",
                      basis=["근로기준법 §11·시행령 별표1 — 상시 4인 이하 사업장 근로시간 한도 미적용"])
    if guard:
        return _entry(ym, "주52시간", "적법",
                      "감시·단속적 근로 승인(§63) — 근로시간·휴게·휴일 규정 미적용",
                      basis=["근로기준법 §63 — 고용노동부장관 승인 감시·단속적 근로자"],
                      cautions=["§63 승인의 유효 여부(승인서)를 확인할 것. 야간 가산(§56③)은 "
                                "§63 근로자에게도 적용된다."])
    weeks = mon.get("주별근로시간")
    if weeks:
        r = calc.weekly_52h_check(weeks, weekly_statutory=min(weekly, lc.STATUTORY_WEEKLY_HOURS),
                                  flex_mode=flex)
        res = r["결과"]
        요지 = (f"주별 근로시간 검사 — 한도 주 {_fmt(res['주한도'])}시간, "
               f"위반 주차 {res['위반주차'] or '없음'}")
        return _entry(ym, "주52시간", res["판정"], 요지,
                      steps=r["계산과정"], basis=r["근거"], cautions=r["주의사항"],
                      위반주차=res["위반주차"], 주한도=res["주한도"],
                      평균주시간=res["평균주시간"], 주별상세=res["주별상세"])
    ot = mon.get("연장시간")
    if ot is None:
        return _entry(ym, "주52시간", "불확실",
                      "주별 근로시간·월 연장시간 모두 기재 없음 — 판정 불가",
                      cautions=["주별 근로시간 또는 월 연장시간 데이터가 있어야 검사 가능."])
    # 주별 데이터가 없으면 월 연장시간으로 추정 — 한도 초과 추정이어도 '불확실'로만 판정
    monthly_limit = lc.MAX_WEEKLY_OVERTIME * lc.AVG_WEEKS_PER_MONTH
    weekly_avg = ot / lc.AVG_WEEKS_PER_MONTH
    steps = [f"월 연장 {_fmt(float(ot))}시간 ÷ {lc.AVG_WEEKS_PER_MONTH:.3f}주 "
             f"= 주 평균 연장 {weekly_avg:.1f}시간 (한도 주 {lc.MAX_WEEKLY_OVERTIME}시간)"]
    basis = ["근로기준법 §53① — 합의 연장 주 12시간 한도"]
    if ot > monthly_limit + 1e-9:
        return _entry(ym, "주52시간", "불확실",
                      f"월 연장 {_fmt(float(ot))}시간 → 주 평균 {weekly_avg:.1f}시간으로 한도 초과 추정 "
                      "— 주별 데이터 없어 불확실",
                      steps=steps, basis=basis,
                      cautions=["주별 근로시간 데이터가 없어 추정 판정 — §53① 위반이 강하게 "
                                "의심되므로 주별 기록으로 확정할 것."])
    return _entry(ym, "주52시간", "적법",
                  f"월 연장 {_fmt(float(ot))}시간 (주 평균 {weekly_avg:.1f}시간 ≤ 한도) — "
                  "주별 데이터 없어 특정 주 초과 여부는 확인 불가",
                  steps=steps, basis=basis,
                  cautions=["월 합계 기준 추정 — 특정 주에 몰아서 근로했다면 초과 가능성이 "
                            "남으므로 주별 기록 확인 권장."])


# ---------------------------------------------------------------------------
# 검사 ⑤ 가산수당 미지급
# ---------------------------------------------------------------------------

def _check_overtime_paid(ym, mon, items, hourly, n_emp, under5, guard):
    paid = sum(int(it.get("금액", 0)) for it in items if it.get("성격") in _OT_PAY_KINDS)
    if under5:
        return _entry(ym, "가산수당", "적법",
                      f"상시 {n_emp}인 — §56 미적용, 가산 의무 없음",
                      basis=["근로기준법 §11·시행령 별표1 — 상시 4인 이하 사업장 §56 미적용"],
                      cautions=["가산은 없지만 실제 근로한 연장·휴일 시간분 임금(100%)은 지급 "
                                "대상 — 기본급·수당에 포함됐는지 별도 확인 필요."],
                      실지급연장수당류=paid)
    ot, night, hol = mon.get("연장시간"), mon.get("야간시간"), mon.get("휴일시간")
    if guard:
        # §63 근로자 — 연장·휴일 가산 미적용, 야간 가산(§56③)만 검증
        if night is None:
            return _entry(ym, "가산수당", "불확실",
                          "야간근로시간 기재 없음 — 야간 가산 검증 불가 "
                          "(§63 근로자는 연장·휴일 가산 미적용, 야간 가산만 적용)",
                          basis=["근로기준법 §63·§56③"])
        r = calc.calc_overtime_pay(hourly, 0, float(night), 0, employees=n_emp)
        theo = r["결과"]["합계"]
        shortfall = round(theo - paid, 2)
        violation = shortfall > 1.0
        return _entry(ym, "가산수당", "위반" if violation else "적법",
                      f"§63 근로자 야간 가산 이론치 {_fmt(theo)}원 vs 실지급 {_fmt(paid)}원",
                      steps=r["계산과정"], basis=r["근거"] + ["근로기준법 §63 — 연장·휴일 가산 미적용"],
                      cautions=r["주의사항"],
                      이론치=round(theo, 2), 실지급액=paid,
                      부족액=shortfall if violation else 0)
    if ot is None and night is None and hol is None:
        return _entry(ym, "가산수당", "불확실",
                      "연장·야간·휴일 시간 기재 없음 — 가산수당 검증 불가",
                      cautions=["시행령 §27 8호(연장·야간·휴일 시간수) 기재가 있어야 검증 가능."])
    if hourly <= 0:
        return _entry(ym, "가산수당", "불확실",
                      "통상시급 산출 불가(통상임금성 임금항목 없음) — 이론치 계산 불가")
    ot0, night0, hol0 = float(ot or 0), float(night or 0), float(hol or 0)
    # 휴일은 월 합산 시간이라 calc_overtime_pay의 '8시간 초과 자동 분리'(1일 전제)를 쓰면
    # 과대 추정 → 과잉 위반 판정 위험. 전부 8시간 이내(1.5배) 하한으로 계산한다.
    r = calc.calc_overtime_pay(hourly, ot0, night0, 0, employees=n_emp)
    hol_mult = 1 + lc.HOLIDAY_PREMIUM_WITHIN_8H
    hol_floor = hourly * hol_mult * hol0
    theo = r["결과"]["합계"] + hol_floor
    steps = list(r["계산과정"])
    if hol0:
        steps.append(f"휴일수당(월 합산, 8시간 이내 가정) = {_fmt(hol0)}시간 × {_fmt(hourly)} × "
                     f"{hol_mult} = {_fmt(hol_floor)}원")
    cautions = list(r["주의사항"])
    if (ot is None or night is None or hol is None):
        cautions.append("일부 시간 필드 미기재(null)는 0시간으로 간주 — 실제 근로가 있었다면 "
                        "이론치가 과소 추정된다.")
    if hol0 > 8:
        cautions.append("휴일 8시간 초과분(100% 가산)은 일별 데이터가 없어 미반영 — "
                        "이론치는 하한(과소 추정) 기준.")
    shortfall = round(theo - paid, 2)
    violation = shortfall > 1.0
    요지 = (f"가산수당 이론치 {_fmt(round(theo, 2))}원 vs 실지급 {_fmt(paid)}원 — "
           f"{'부족 ' + _fmt(shortfall) + '원' if violation else '충족'}")
    return _entry(ym, "가산수당", "위반" if violation else "적법", 요지,
                  steps=steps, basis=r["근거"], cautions=cautions,
                  이론치=round(theo, 2), 실지급액=paid,
                  부족액=shortfall if violation else 0)


# ---------------------------------------------------------------------------
# 검사 ⑥ 임금대장 기재사항 누락 (시행령 §27)
# ---------------------------------------------------------------------------

def _check_ledger_fields(ym, mon, under5, guard, short_daily):
    missing, lawful = [], []
    if mon.get("근로일수") is None:
        missing.append("6호(근로일수)")
    hours_exempt = under5 or guard
    if mon.get("총근로시간") is None:
        (lawful if hours_exempt else missing).append("7호(근로시간수)")
    if mon.get("연장시간") is None and mon.get("야간시간") is None and mon.get("휴일시간") is None:
        (lawful if hours_exempt else missing).append("8호(연장·야간·휴일 시간수)")
    if not (mon.get("임금항목") or []):
        missing.append("9호(기본급·수당 내역별 금액)")

    cautions = ["1~5호(성명·생년월일·업무·임금 계산기초 등)와 10호(공제 — 공제한 경우에만 "
                "기재)는 정규화 데이터만으로 판정 불가 — 대장 원본 확인 필요."]
    if lawful:
        cautions.append("4인 이하 사업장·§63 승인 근로자는 7·8호(근로시간류) 기재 생략이 "
                        "적법하다 (시행령 §27③) — 누락으로 보지 않음.")
    if short_daily:
        cautions.append("고용기간 30일 미만 — 2호(생년월일·사원번호 등)·5호(임금 계산기초) "
                        "생략도 적법 (시행령 §27②).")
    verdict = "위반" if missing else "적법"
    요지 = ("누락: " + ", ".join(missing)) if missing else \
        ("기재사항 충족" + (f" (생략 적법: {', '.join(lawful)})" if lawful else ""))
    return _entry(ym, "기재사항", verdict, 요지,
                  basis=["근로기준법 §48①·시행령 §27 — 임금대장 기재사항 10개 호",
                         "근로기준법 §116② — 위반 시 과태료 500만원 이하"],
                  cautions=cautions, 누락호=missing, 생략적법=lawful)


# ---------------------------------------------------------------------------
# 검사 ③ 평균임금 (퇴사자 — 퇴직 시점 기준)
# ---------------------------------------------------------------------------

def _check_average_wage(emp, weekly):
    사유일 = date.fromisoformat(emp["퇴사일"])
    by_ym = {m["연월"]: m for m in emp.get("월별", []) if m.get("연월")}
    m0 = f"{(사유일 - timedelta(days=1)).year:04d}-{(사유일 - timedelta(days=1)).month:02d}"
    needed = [_ym_add(m0, -2), _ym_add(m0, -1), m0]
    window_items = [by_ym[x].get("임금항목") or [] for x in needed if x in by_ym]
    missing_yms = [x for x in needed if x not in by_ym]
    cautions = []
    if not window_items:
        return _entry(None, "평균임금", "불확실",
                      f"퇴직({emp['퇴사일']}) 이전 3개월({needed[0]}~{needed[2]})의 임금 데이터 없음 — 산정 불가")
    days = (사유일 - _first_day(needed[0])).days
    if 사유일.day != 1:
        cautions.append("퇴사일이 월 초일이 아님 — 월 단위 임금대장으로는 일할 창구를 정확히 "
                        "반영할 수 없어 마지막 달 전체 지급액 기준 근사치.")

    # 연간 상여·연차수당: 대장에 있는 최근 12개월분 합계 → calc_average_wage가 3/12 산입
    lookback = {_ym_add(m0, -k) for k in range(12)}
    bonus_total = leave_total = 0
    for ym in lookback & by_ym.keys():
        for it in by_ym[ym].get("임금항목") or []:
            if it.get("성격") == "정기상여" and it.get("지급주기", "매월") != "매월":
                bonus_total += int(it.get("금액", 0))
            elif it.get("성격") == "연차수당":
                leave_total += int(it.get("금액", 0))
    if len(lookback & by_ym.keys()) < 12 and (bonus_total or leave_total):
        cautions.append("연간 상여·연차수당 합계는 대장에 있는 12개월 미만 데이터 기준 — "
                        "대장 밖 지급분이 있으면 보정 필요.")

    # 통상임금 하한 비교용 — 마지막 달(없으면 창구 내 최근 달) 기준
    ow_ym = m0 if m0 in by_ym else max(x for x in needed if x in by_ym)
    ow = calc.calc_ordinary_wage(by_ym[ow_ym].get("임금항목") or [], weekly, int(ow_ym[:4]),
                                 as_of_date=f"{ow_ym}-01")
    r = calc.calc_average_wage(window_items, bonus_total, leave_total, days,
                               ordinary_wage_monthly=ow["결과"]["월통상임금"],
                               weekly_hours=weekly)
    res = r["결과"]
    verdict = "불확실" if missing_yms else "적법"
    if missing_yms:
        cautions.append(f"산정 창구 중 {', '.join(missing_yms)} 데이터 없음 — "
                        f"{len(window_items)}개월분만으로 산정한 근사치.")
    요지 = (f"1일 평균임금 {_fmt(res['1일평균임금'])}원 → 적용 {_fmt(res['적용평균임금'])}원"
           + (" (통상임금 하한 적용)" if res["통상임금하한적용"] else ""))
    return _entry(None, "평균임금", verdict, 요지,
                  steps=r["계산과정"], basis=r["근거"], cautions=r["주의사항"] + cautions,
                  **{"1일평균임금": res["1일평균임금"], "적용평균임금": res["적용평균임금"],
                     "통상임금하한적용": res["통상임금하한적용"],
                     "1일통상임금": res["1일통상임금"], "임금총액": res["임금총액"],
                     "상여산입액": res["상여산입액"], "연차수당산입액": res["연차수당산입액"],
                     "산정창구": {"대상월": needed, "누락월": missing_yms, "총일수": days},
                     "연간상여합계": bonus_total, "연간연차수당합계": leave_total})


# ---------------------------------------------------------------------------
# 연차 발생 정보 (5인 이상만 — 4인 이하는 §60 미적용이라 검사 자체를 수행하지 않음)
# ---------------------------------------------------------------------------

def _check_annual_leave_info(emp, months):
    hire = emp.get("입사일")
    if not hire or not months:
        return None
    if emp.get("퇴사일"):
        base, employed = emp["퇴사일"], False
    else:
        base, employed = _last_day(months[-1]["연월"]).isoformat(), True
    try:
        r = calc.calc_annual_leave(hire, base, employed_on_base_date=employed)
    except ValueError as e:
        return _entry(None, "연차", "불확실", f"연차 산정 불가 — {e}")
    res = r["결과"]
    요지 = (f"기준일 {base}까지 발생 연차 {_fmt(res['총발생일수'])}일 "
           f"(월개근분 {res['월개근분']}일 + 연차분 {_fmt(res['연차분'])}일 — 개근·출근율 80% 이상 가정)")
    return _entry(None, "연차", "적법", 요지,
                  steps=r["계산과정"], basis=r["근거"],
                  cautions=r["주의사항"] + ["사용·미사용수당 정산 여부는 임금대장만으로 확인 "
                                        "불가 — 발생일수 참고용. 월 개근·출근율은 데이터가 "
                                        "없어 충족으로 가정."],
                  총발생일수=res["총발생일수"], 월개근분=res["월개근분"], 연차분=res["연차분"])


# ---------------------------------------------------------------------------
# analyze_payroll 본체
# ---------------------------------------------------------------------------

def analyze_payroll(data: dict) -> dict:
    """임금대장 일괄 분석 — 직원×월 매트릭스로 6종 검사.

    ① 최저임금 ② 통상임금 산정 ③ 평균임금(퇴사자) ④ 주52시간 ⑤ 가산수당 미지급
    ⑥ 기재사항 누락(시행령 §27) + 연차 발생 정보(5인 이상만).
    출력 구조는 모듈 docstring 참조.
    """
    if not isinstance(data, dict) or "사업장" not in data or "직원" not in data:
        raise ValueError("입력에 '사업장'·'직원' 키가 필요합니다 — "
                         "labor://schema/임금대장-입력 스키마를 따를 것.")
    biz = data["사업장"]
    n_emp = int(biz.get("상시근로자수", 5))
    flex = biz.get("탄력선택근로제") or "없음"
    under5 = n_emp < lc.FULL_APPLICATION_MIN_EMPLOYEES

    직원별 = []
    for emp in data["직원"]:
        hire_d = date.fromisoformat(emp["입사일"]) if emp.get("입사일") else None
        quit_d = date.fromisoformat(emp["퇴사일"]) if emp.get("퇴사일") else None
        short_daily = bool(hire_d and quit_d and (quit_d - hire_d).days < 30)
        weekly = float(emp.get("주소정근로시간", 40.0))
        probation_flag = bool(emp.get("수습여부", False))
        c1yr = bool(emp.get("계약기간1년이상", False))
        simple = bool(emp.get("단순노무직", False))
        guard = bool(emp.get("감시단속승인", False))
        months = sorted((m for m in emp.get("월별", []) if m.get("연월")),
                        key=lambda m: m["연월"])
        entries = []
        for mon in months:
            ym = mon["연월"]
            year = int(ym[:4])
            items = mon.get("임금항목") or []
            entries.append(_check_min_wage(ym, year, items, weekly,
                                           probation_flag, c1yr, simple, hire_d))
            ow_entry, hourly = _check_ordinary(ym, year, items, weekly)
            entries.append(ow_entry)
            entries.append(_check_52h(ym, mon, weekly, under5, guard, flex))
            entries.append(_check_overtime_paid(ym, mon, items, hourly, n_emp, under5, guard))
            entries.append(_check_ledger_fields(ym, mon, under5, guard, short_daily))
        if emp.get("퇴사일") and months:
            entries.append(_check_average_wage(emp, weekly))
        if not under5:
            leave_entry = _check_annual_leave_info(emp, months)
            if leave_entry:
                entries.append(leave_entry)
        직원별.append({"사원ID": emp.get("사원ID"), "성명": emp.get("성명"),
                     "월별판정": entries})

    요약, 경고 = _summarize(직원별, under5, n_emp)
    return {"직원별": 직원별, "요약": 요약, "경고": 경고}


def _summarize(직원별, under5, n_emp):
    counts = {}
    uncertain = 0
    total_checks = 0
    short_total = 0.0
    arrears = False
    for e in 직원별:
        for t in e["월별판정"]:
            total_checks += 1
            if t["판정"] == "위반":
                counts[t["검사항목"]] = counts.get(t["검사항목"], 0) + 1
                if t["검사항목"] in _ARREARS_CHECKS:
                    arrears = True
                short_total += float(t["상세"].get("부족액") or 0)
            elif t["판정"] == "불확실":
                uncertain += 1
    요약 = {"직원수": len(직원별), "총검사건수": total_checks,
           "위반건수": counts, "위반합계": sum(counts.values()),
           "불확실건수": uncertain, "총부족액추정": round(short_total, 2)}
    경고 = []
    if arrears:
        경고.append("임금체불성 위반(최저임금 미달·가산수당 미지급)이 검출되었습니다 — "
                   "상습 임금체불 제재(2025-10-23 시행 개정 근로기준법: 반의사불벌 배제, "
                   "신용제재, 최대 3배 손해배상, 재직자 지연이자)의 대상이 될 수 있습니다.")
    elif counts:
        경고.append("위반 항목이 검출되었습니다 — 근거·계산과정을 확인하고 시정할 것.")
    if under5:
        경고.append(f"상시 {n_emp}인 사업장 — 연차(§60)·가산수당(§56)·근로시간 한도(§50~53) "
                   "등은 적용되지 않아 해당 검사는 미수행 또는 '적법(미적용)' 처리했습니다 "
                   "(근기법 §11·시행령 별표1).")
    경고.append("분석 결과는 입력된 임금대장 데이터(항목 분류·근로시간 기록)의 정확성에 "
               "종속됩니다 — '불확실' 항목은 원자료·법리 확인이 필요합니다.")
    return 요약, 경고


# ---------------------------------------------------------------------------
# design_pay_table — 급여테이블 역산 설계
# ---------------------------------------------------------------------------

def design_pay_table(cond: dict) -> dict:
    """설계 조건 → 기본급+고정OT 분해 → 최저임금 역검증 → 근로계약서 임금조항 문안.

    통상시급이 기본급에 의존하고 고정OT가 통상시급에 의존하는 순환은
    T = X + F + B + (X + C)·K/H 를 X에 대해 대수적으로 풀어 해소한다
    (X=기본급, F=매월 수당, B=매월 상여, C=통상임금성 수당·상여 월 환산, K=가산 환산계수).

    반환: {"급여테이블": {...}, "검증": {"최저임금판정", "상세"},
           "계약서_임금조항_문안": str, "계산과정": [], "근거": [], "주의사항": []}
    설계 불능(목표 금액이 수당·OT를 감당 못함)·잘못된 방식/주기는 ValueError.
    """
    year = int(cond["연도"])
    n_emp = int(cond.get("상시근로자수", 5))
    weekly = float(cond.get("주소정근로시간", 40.0))
    ot_h = float(cond.get("고정연장시간_월") or 0)
    night_h = float(cond.get("고정야간시간_월") or 0)
    hol_h = float(cond.get("고정휴일시간_월") or 0)
    goal = cond.get("목표") or {}
    method = goal.get("방식")
    if method not in ("월총액", "기본급", "연봉"):
        raise ValueError("목표.방식은 '월총액'|'기본급'|'연봉' 중 하나여야 합니다.")
    amount = int(goal.get("금액", 0))
    if amount <= 0:
        raise ValueError("목표.금액은 양수여야 합니다.")
    allowances = cond.get("고정수당") or []
    bonus = cond.get("정기상여")
    probation = bool(cond.get("수습적용", False))
    c1yr = bool(cond.get("계약기간1년이상", False))
    simple = bool(cond.get("단순노무직", False))

    steps, basis, cautions = [], [], []
    H = lc.monthly_standard_hours(weekly)
    under5 = n_emp < lc.FULL_APPLICATION_MIN_EMPLOYEES
    if under5:
        mult = {"연장": 1.0, "야간": 0.0, "휴일": 1.0}
        cautions.append("상시 5인 미만 — §56 가산이 적용되지 않아 고정OT를 가산 없는 100% "
                        "단가로 설계했다 (야간 시간분은 소정·연장 임금에 이미 포함되므로 0). "
                        "상시 5인 이상이 되면 가산 전제 자체가 달라져 즉시 재설계가 필요하다.")
        basis.append("근로기준법 §11·시행령 별표1 — 상시 4인 이하 사업장 §56 미적용")
    else:
        mult = {"연장": 1 + lc.OVERTIME_PREMIUM, "야간": lc.NIGHT_PREMIUM,
                "휴일": 1 + lc.HOLIDAY_PREMIUM_WITHIN_8H}
    K = mult["연장"] * ot_h + mult["야간"] * night_h + mult["휴일"] * hol_h
    steps.append(f"월 기준시간(주휴 포함) H = {_fmt(H)}시간 (주 {weekly:g}시간)")
    steps.append(f"고정OT 환산계수 K = 연장 {ot_h:g}h×{mult['연장']:g} + "
                 f"야간 {night_h:g}h×{mult['야간']:g} + 휴일 {hol_h:g}h×{mult['휴일']:g} = {K:g}")

    # 고정수당 — 통상임금성 월 환산분(F_ow)과 매월 현금 지급분(F_paid)을 분리
    F_paid = 0
    F_ow = 0.0
    allowance_rows = []
    uncertain_names = []
    for a in allowances:
        name = a.get("명칭", "(무명)")
        amt = int(a.get("금액", 0))
        period = a.get("지급주기", "매월")
        cls = calc.classify_wage_item(a, year)
        months = _PERIOD_MONTHS.get(period)
        ow_m = 0.0
        if cls["통상임금"].startswith("포함") and months:
            ow_m = cls["통상임금산입액"] / months
            F_ow += ow_m
        elif cls["통상임금"].startswith("불확실"):
            uncertain_names.append(name)
        if period == "매월":
            F_paid += amt
        allowance_rows.append({"명칭": name, "금액": amt, "지급주기": period,
                               "성격": a.get("성격"), "통상임금월산입액": round(ow_m, 2),
                               "통상임금판정": cls["통상임금"]})
    if uncertain_names:
        cautions.append(f"통상임금 포함 여부 불확실 항목({', '.join(uncertain_names)})은 "
                        "통상시급 산정에서 제외했다 — 포함으로 판정되면 통상시급·고정OT가 "
                        "올라 미달이 생길 수 있으므로 보수적으로는 포함 설계를 권장.")

    b_annual, b_period, b_ow_m, b_paid = 0, None, 0.0, 0.0
    if bonus:
        b_annual = int(bonus.get("연간총액", 0))
        b_period = bonus.get("지급주기", "연간")
        if b_period not in _PERIOD_MONTHS:
            raise ValueError("정기상여.지급주기는 매월|격월|분기|반기|연간 중 하나여야 합니다.")
        b_ow_m = b_annual / 12
        if b_period == "매월":
            b_paid = b_annual / 12
        steps.append(f"정기상여 연 {_fmt(b_annual)}원({b_period}) → 월 환산 {_fmt(b_ow_m)}원 "
                     "통상임금 산입 (2024-12-19 전합 신법리 — 지급주기·조건 무관)")
    C = F_ow + b_ow_m

    if method == "기본급":
        X = float(amount)
        steps.append(f"기본급 지정 방식 — 기본급 {_fmt(amount)}원에서 전개")
    else:
        if method == "연봉":
            nonmonthly_bonus = b_annual if (bonus and b_period != "매월") else 0
            T = (amount - nonmonthly_bonus) / 12
            steps.append(f"연봉 {_fmt(amount)}원 − 비매월 상여 {_fmt(nonmonthly_bonus)}원 "
                         f"→ 월총액 목표 {_fmt(T)}원")
        else:
            T = float(amount)
        X = (T - F_paid - b_paid - C * K / H) / (1 + K / H)
        steps.append(f"기본급 X = (월총액 {_fmt(T)} − 매월 수당 {_fmt(F_paid)} − 매월 상여 "
                     f"{_fmt(b_paid)} − 통상수당분 OT {_fmt(C * K / H)}) ÷ (1 + {K:g}/{_fmt(H)}) "
                     f"= {X:,.2f}원")
        if X <= 0:
            raise ValueError("목표 금액이 고정수당·상여·고정OT 부담을 감당하지 못합니다 — "
                             "목표 상향 또는 구성 축소가 필요합니다.")

    ow_monthly = X + C
    hourly = ow_monthly / H
    ot_pay = hourly * mult["연장"] * ot_h
    night_pay = hourly * mult["야간"] * night_h
    hol_pay = hourly * mult["휴일"] * hol_h
    fixed_ot = ot_pay + night_pay + hol_pay
    monthly_total = X + F_paid + b_paid + fixed_ot
    nonmonthly_allow_annual = sum(
        r["금액"] * 12 / _PERIOD_MONTHS[r["지급주기"]]
        for r in allowance_rows
        if r["지급주기"] in _PERIOD_MONTHS and r["지급주기"] != "매월")
    annual_total = (monthly_total * 12 + nonmonthly_allow_annual
                    + (b_annual if bonus and b_period != "매월" else 0))
    steps.append(f"월 통상임금 = 기본급 {X:,.2f} + 통상임금성 수당·상여 {_fmt(C)} "
                 f"= {ow_monthly:,.2f}원 → 통상시급 = {ow_monthly:,.2f} ÷ {_fmt(H)} "
                 f"= {hourly:,.2f}원")
    if ot_h:
        steps.append(f"고정연장수당 = {hourly:,.2f} × {ot_h:g}h × {mult['연장']:g} = {ot_pay:,.2f}원")
    if night_h:
        steps.append(f"고정야간수당 = {hourly:,.2f} × {night_h:g}h × {mult['야간']:g} = {night_pay:,.2f}원")
    if hol_h:
        steps.append(f"고정휴일수당 = {hourly:,.2f} × {hol_h:g}h × {mult['휴일']:g} = {hol_pay:,.2f}원")
    steps.append(f"월 지급 총액 = {X:,.2f} + {_fmt(F_paid)} + {_fmt(b_paid)} + {fixed_ot:,.2f} "
                 f"= {monthly_total:,.2f}원")

    # 최저임금 역검증 — 분해된 테이블을 그대로 check_minimum_wage에 통과 (수습 3요건 반영)
    verify_items = [{"명칭": "기본급", "금액": int(round(X)), "지급주기": "매월", "성격": "기본급"}]
    verify_items += [dict(a) for a in allowances]
    if bonus and b_annual:
        per_pay = int(round(b_annual * _PERIOD_MONTHS[b_period] / 12))
        verify_items.append({"명칭": "정기상여", "금액": per_pay,
                             "지급주기": b_period, "성격": "정기상여"})
    for name, pay, kind in (("고정연장수당", ot_pay, "연장수당"),
                            ("고정야간수당", night_pay, "야간수당"),
                            ("고정휴일수당", hol_pay, "휴일수당")):
        if pay:
            verify_items.append({"명칭": name, "금액": int(round(pay)),
                                 "지급주기": "매월", "성격": kind})
    verify = calc.check_minimum_wage(verify_items, weekly, year, probation=probation,
                                     contract_1yr_plus=c1yr, simple_labor=simple)
    vres = verify["결과"]
    if vres["위반여부"]:
        cautions.append(f"설계안이 최저임금 미달 — 비교시급 {_fmt(vres['비교시급'])}원 < "
                        f"적용 최저시급 {_fmt(vres['적용최저시급'])}원, 월 부족액 "
                        f"{_fmt(vres['월부족액'])}원. 목표 금액 상향 또는 고정OT 시간 축소 필요.")
    cautions += [c for c in verify["주의사항"] if c not in cautions]

    ot_parts = [(n, p, h, m) for n, p, h, m in
                (("고정연장근로수당", ot_pay, ot_h, mult["연장"]),
                 ("고정야간근로수당", night_pay, night_h, mult["야간"]),
                 ("고정휴일근로수당", hol_pay, hol_h, mult["휴일"])) if p]
    문안 = _wage_clause(X, allowance_rows, bonus, b_annual, b_period, ot_parts,
                       hourly, ow_monthly, H, weekly, monthly_total)

    cautions.append("통상임금을 줄일 목적의 항목 쪼개기(상여 분리·재직조건부 수당화)는 "
                    "2024-12-19 전원합의체(2020다247190·2023다302838) 이후 법적 실익이 없다 "
                    "— 정기·일률 지급분은 조건과 무관하게 통상임금이다.")
    cautions.append("고정OT는 '시간 수'를 근로계약서에 반드시 명시할 것 — 시간 미기재 "
                    "포괄임금 약정은 무효 위험이 있고, 실근로가 고정시간을 초과하면 "
                    "그 차액을 별도 지급해야 한다.")
    if hol_h:
        cautions.append("고정휴일시간은 1일 8시간 이내 근로 가정(1.5배) — 8시간 초과 "
                        "휴일근로가 예정되면 2.0배로 재산정 필요.")
    cautions.append("식대 등 복리후생 항목의 비과세 한도는 소득세법 영역 — 국세 MCP"
                    "(Korea Law 커넥터)로 확인할 것.")
    basis += [f"최저임금 고시 — {year}년 시급 {_fmt(vres['최저시급'])}원",
              "근로기준법 §17② — 임금 구성항목·계산방법·지급방법 서면 명시·교부",
              "근로기준법 §56·시행령 §6 — 가산수당·통상임금 시간급 환산",
              "대법원 전원합의체 2024-12-19 선고 2020다247190·2023다302838 — 통상임금 신법리"]

    return {
        "급여테이블": {
            "기본급": round(X, 2),
            "고정수당": allowance_rows,
            "정기상여": ({"연간총액": b_annual, "지급주기": b_period,
                       "월환산액": round(b_ow_m, 2), "월지급액": round(b_paid, 2)}
                      if bonus else None),
            "고정연장수당": round(ot_pay, 2),
            "고정야간수당": round(night_pay, 2),
            "고정휴일수당": round(hol_pay, 2),
            "고정OT합계": round(fixed_ot, 2),
            "고정OT시간": {"연장": ot_h, "야간": night_h, "휴일": hol_h},
            "가산배율": mult,
            "월지급총액": round(monthly_total, 2),
            "연봉환산": round(annual_total, 2),
            "월통상임금": round(ow_monthly, 2),
            "통상시급": round(hourly, 2),
            "월기준시간": H,
        },
        "검증": {"최저임금판정": "위반" if vres["위반여부"] else "적법", "상세": vres},
        "계약서_임금조항_문안": 문안,
        "계산과정": steps,
        "근거": basis,
        "주의사항": cautions,
    }


def _wage_clause(X, allowance_rows, bonus, b_annual, b_period, ot_parts,
                 hourly, ow_monthly, H, weekly, monthly_total):
    """근기법 §17② 서면 명시·교부 형식의 임금조항 문안 (구성항목·계산방법·지급방법)."""
    lines = ["제 O조(임금)",
             f"① 임금의 구성항목 (월 지급 총액 {_fmt(round(monthly_total))}원):"]
    idx = 1
    lines.append(f"  {idx}. 기본급: 월 {_fmt(round(X))}원")
    for a in allowance_rows:
        idx += 1
        lines.append(f"  {idx}. {a['명칭']}: {a['금액']:,}원 ({a['지급주기']} 지급)")
    if bonus and b_annual:
        idx += 1
        lines.append(f"  {idx}. 정기상여: 연 {_fmt(b_annual)}원 ({b_period} 분할 지급)")
    for name, pay, h, m in ot_parts:
        idx += 1
        lines.append(f"  {idx}. {name}: 월 {_fmt(round(pay))}원 (월 {h:g}시간분)")
    lines.append("② 계산방법:")
    lines.append(f"  - 월 통상임금 {_fmt(round(ow_monthly))}원, 월 소정근로시간 {_fmt(H)}시간"
                 f"(주 {weekly:g}시간, 주휴 포함)")
    lines.append(f"  - 통상시급 = {_fmt(round(ow_monthly))}원 ÷ {_fmt(H)}시간 = {hourly:,.2f}원")
    for name, pay, h, m in ot_parts:
        lines.append(f"  - {name} = 통상시급 × {h:g}시간 × {m:g} = {_fmt(round(pay))}원")
    if ot_parts:
        lines.append("  - 위 고정 시간을 초과하는 연장·야간·휴일 실근로에 대하여는 "
                     "근로기준법 제56조에 따라 가산수당을 별도 지급한다.")
    lines.append("③ 지급방법: 임금은 매월 1회 일정한 날짜(매월  일)에 근로자 명의 계좌로 "
                 "전액 지급하고, 지급 시 임금명세서를 서면(전자문서 포함)으로 교부한다. "
                 "(근로기준법 제43조·제48조②)")
    return "\n".join(lines)
