# -*- coding: utf-8 -*-
"""
social_insurance.py — 4대보험(건강보험·장기요양·국민연금·고용보험) 파라미터와 보험료 계산

노무 계산(calculators.py)과 달리 **연도별 파라미터가 법령·고시에 흩어져 있고 해마다
바뀐다**. 요율표와 계산식을 한 파일에 둔 이유가 그것이다 — 매년 갱신이 이 파일 하나로
끝나야 하고, 요율 옆에 그 요율을 쓰는 식이 바로 보여야 잘못 짝지을 일이 없다.

모든 수치는 2026-09-12에 law.go.kr Open API로 **법령·고시 원문을 직접 읽어** 확정했다.
출처를 값 옆에 적어 두었으니 갱신할 때도 같은 방식으로 확인할 것 (verify_rates()가
현행 법령과 표를 대조한다 — check_sources_health에서 호출).

특히 주의할 세 가지 — 셋 다 "당연히 이럴 것"이라고 넘겨짚으면 틀린다:

1. **국민연금 요율은 2026년부터 매년 오른다.** 국민연금법 제88조③ 본칙은 기여금·부담금
   각 6.5%(합계 13%)지만, 부칙(법률 제20903호, 2025.4.2 공포) 제4조 특례가 2026~2032년을
   따로 정한다 — 2026년은 각 4.75%(합계 9.5%). 본칙만 보면 0.25%p ~ 1.75%p 틀린다.
2. **장기요양보험료 산정 기준이 2023년에 바뀌었다.** 2022년까지는 '건강보험료 × 요율',
   2023년부터는 '건강보험료 × (장기요양보험료율 ÷ 건강보험료율)'이고 시행령의 요율 자체가
   소득 대비로 표기된다 (노인장기요양보험법 제9조①).
3. **국민연금 기준소득월액 상·하한의 적용기간은 7월~다음 해 6월**이다 (국민연금법 시행령
   제5조④). 달력연도로 잡으면 상·하한이 반년씩 밀린다.
"""
from datetime import date
from decimal import Decimal, ROUND_FLOOR

__all__ = [
    "health_rate", "ltc_rate", "ltc_ratio", "pension_rate", "pension_income_band",
    "health_premium_cap", "ei_unemployment_rate", "ei_stability_rate",
    "calc_social_insurance", "check_premium_support", "verify_rates",
    "SUPPORTED_YEARS",
]


def _d(x) -> date:
    """'2026-01-01' · '20260101' · date → date. 못 읽으면 ValueError."""
    if isinstance(x, date):
        return x
    s = str(x or "").strip().replace(".", "-").replace("/", "-")
    if s.isdigit() and len(s) == 8:
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        y, m, dd = (int(v) for v in s.split("-")[:3])
        return date(y, m, dd)
    except Exception as e:                                     # noqa: BLE001
        raise ValueError(f"날짜 형식 오류: {x!r} (YYYY-MM-DD 또는 YYYYMMDD)") from e


def _pct(rate) -> str:
    """요율 표시 — 0.0719 → '7.19%', 0.009448 → '0.9448%'. 꼬리 0을 달고 나오면 읽기 나쁘다."""
    s = f"{_dec(rate) * 100:f}".rstrip("0").rstrip(".")
    return f"{s or '0'}%"


def _missing(kind: str, year: int, table) -> ValueError:
    have = ", ".join(str(y) for y in sorted(table))
    return ValueError(
        f"{year}년 {kind}이(가) 파라미터 표에 없습니다 (보유: {have}). "
        f"social_insurance.py의 표에 법령·고시 값을 추가하세요 — 조용히 최신값을 쓰면 "
        f"보험료가 통째로 틀어지므로 일부러 막아 둡니다.")


# ---------------------------------------------------------------------------
# 건강보험료율 — 국민건강보험법 시행령 제44조① (보수월액 대비, 노사 합계)
# 값은 시행령 연혁(law_article_as_of)에서 확인. '1만분의 N' → N/10000.
# ---------------------------------------------------------------------------
HEALTH_RATE = {
    2017: 0.0612, 2018: 0.0624, 2019: 0.0646, 2020: 0.0667, 2021: 0.0686,
    2022: 0.0699, 2023: 0.0709, 2024: 0.0709, 2025: 0.0709, 2026: 0.0719,
}

# 장기요양보험료율 — 노인장기요양보험법 시행령 제4조
# 2022년까지: '1만분의 N' = 건강보험료 대비 비율
# 2023년부터: '100만분의 N' = 소득(보수월액) 대비 비율  ← 기준이 바뀌었다
LTC_RATE = {
    2017: 0.0655, 2018: 0.0738, 2019: 0.0851, 2020: 0.1025, 2021: 0.1152,
    2022: 0.1227,                                   # ↑ 건강보험료 대비
    2023: 0.009082, 2024: 0.009182, 2025: 0.009182, 2026: 0.009448,   # ↑ 소득 대비
}
LTC_BASE_CHANGED_YEAR = 2023          # 이 해부터 소득 대비 표기

# 국민연금 사업장가입자 기여금·부담금 **각각**의 요율 (기준소득월액 대비).
# 2025년까지: 국민연금법 제88조③ 본칙 4.5%
# 2026~2032년: 부칙(법률 제20903호) 제4조① 특례 — 아래 값
# 2033년부터: 개정 본칙 6.5% (합계 13%)
PENSION_RATE_EACH = {
    **{y: 0.045 for y in range(2010, 2026)},
    2026: 0.0475, 2027: 0.0500, 2028: 0.0525, 2029: 0.0550,
    2030: 0.0575, 2031: 0.0600, 2032: 0.0625,
}
PENSION_RATE_AFTER_2032 = 0.065       # 제88조③ 본칙

# 국민연금 기준소득월액 (하한, 상한) — 보건복지부 고시 「국민연금 기준소득월액 하한액과 상한액」
# 키는 **적용 시작 연도**이고 적용기간은 그 해 7월 ~ 다음 해 6월 (시행령 제5조④).
PENSION_INCOME_BAND = {
    2015: (270_000, 4_210_000), 2016: (280_000, 4_340_000), 2017: (290_000, 4_490_000),
    2018: (300_000, 4_680_000), 2019: (310_000, 4_860_000), 2020: (320_000, 5_030_000),
    2021: (330_000, 5_240_000), 2022: (350_000, 5_530_000), 2023: (370_000, 5_900_000),
    2024: (390_000, 6_170_000), 2025: (400_000, 6_370_000), 2026: (410_000, 6_590_000),
}

# 직장가입자 보수월액보험료의 월별 (상한, 하한) — 보건복지부 고시
# 「월별 건강보험료액의 상한과 하한에 관한 고시」. 노사 합계 기준 금액이다.
HEALTH_PREMIUM_CAP = {
    2018: (6_193_140, 17_460), 2019: (6_365_520, 18_020), 2020: (6_644_340, 18_600),
    2021: (7_047_900, 19_140), 2022: (7_307_100, 19_500), 2023: (7_822_560, 19_780),
    2024: (8_481_420, 19_780), 2025: (9_008_340, 19_780), 2026: (9_183_480, 20_160),
}

# 고용보험 실업급여 보험료율 (노사 합계) — 징수법 시행령 제12조①2호.
# 연중에 바뀌므로 **시행일 기준** 구간으로 둔다 (연도 키로 잡으면 2019·2022년이 틀린다).
EI_UNEMPLOYMENT_STEPS = [
    (date(2013, 7, 1), 0.013),
    (date(2019, 10, 1), 0.016),
    (date(2022, 7, 1), 0.018),
]

# 고용안정·직업능력개발사업 보험료율 — 징수법 시행령 제12조①1호. **사업주 전액 부담.**
EI_STABILITY_RATES = [
    ("150명 미만", 0.0025),
    ("150명 이상 우선지원대상기업", 0.0045),
    ("150명 이상 1천명 미만", 0.0065),
    ("1천명 이상·국가·지방자치단체", 0.0085),
]

# 두루누리 — 고용보험료 지원: 고용노동부 고시 「고용보험료의 지원 대상 및 지원 수준 등에 관한 고시」
#            연금보험료 지원: 보건복지부 고시 「소규모사업장 저소득근로자에 대한 연금보험료 지원 등에 관한 고시」
# 두 고시의 인원·보수·재산·소득 요건이 같고 지원율(80%)도 같다. 상한액만 다르다.
# **건강보험·장기요양은 지원 대상이 아니다** (근거 법률 자체가 없다).
PREMIUM_SUPPORT = {
    "근로자수_미만": 10,              # 징수법 시행령 제28조①1호 — 월평균 10명 미만
    "월보수_미만": 2_700_000,         # 고용보험 고시 제4조 / 연금 고시 제2조
    "재산과세표준_미만": 600_000_000,  # 고용보험 고시 제5조① / 연금 고시 제3조
    "종합소득_미만": 43_000_000,      # 고용보험 고시 제5조② / 연금 고시 제4조
    "지원율": 0.8,                    # 근로자분·사업주분 각각 80%
    "고용보험_월한도_근로자": 16_560,
    "고용보험_월한도_사업주": 21_160,
    "연금_월한도": 87_400,            # 근로자분·사업주분 각각의 한도
    "지원기간_개월": 36,              # 2018-01-01 이후 누적
}

SUPPORTED_YEARS = sorted(set(HEALTH_RATE) & set(LTC_RATE))


# ---------------------------------------------------------------------------
# 파라미터 조회
# ---------------------------------------------------------------------------

def health_rate(year: int) -> float:
    """보수월액 대비 건강보험료율(노사 합계). 국민건강보험법 시행령 제44조①."""
    if year not in HEALTH_RATE:
        raise _missing("건강보험료율", year, HEALTH_RATE)
    return HEALTH_RATE[year]


def ltc_rate(year: int) -> float:
    """장기요양보험료율 원값. 2023년 이후는 소득 대비, 그 전은 건강보험료 대비."""
    if year not in LTC_RATE:
        raise _missing("장기요양보험료율", year, LTC_RATE)
    return LTC_RATE[year]


def ltc_ratio(year: int) -> float:
    """**건강보험료에 곱할** 장기요양 비율.

    2023년부터는 노인장기요양보험법 제9조①에 따라 (장기요양보험료율 ÷ 건강보험료율)이며
    소수점 이하 다섯째자리에서 반올림한다(=넷째자리까지 남긴다). 2022년 이전 시행령 값은
    이미 건강보험료 대비라서 그대로 쓴다.
    """
    r = ltc_rate(year)
    if year < LTC_BASE_CHANGED_YEAR:
        return r
    return round(r / health_rate(year), 4)


def pension_rate(year: int) -> float:
    """국민연금 기여금·부담금 **각각**의 요율 (합계는 이 값의 2배)."""
    if year > 2032:
        return PENSION_RATE_AFTER_2032
    if year not in PENSION_RATE_EACH:
        raise _missing("국민연금 보험료율", year, PENSION_RATE_EACH)
    return PENSION_RATE_EACH[year]


def pension_income_band(as_of) -> tuple:
    """기준일이 속한 **적용기간(7월~다음 해 6월)**의 (하한, 상한). 반환 (하한, 상한, 적용연도)."""
    d = _d(as_of)
    y = d.year if d.month >= 7 else d.year - 1
    if y not in PENSION_INCOME_BAND:
        raise _missing("국민연금 기준소득월액 상·하한", y, PENSION_INCOME_BAND)
    lo, hi = PENSION_INCOME_BAND[y]
    return lo, hi, y


def health_premium_cap(year: int) -> tuple:
    """직장 보수월액보험료의 (상한, 하한) — 노사 합계 월액."""
    if year not in HEALTH_PREMIUM_CAP:
        raise _missing("건강보험료 상·하한", year, HEALTH_PREMIUM_CAP)
    return HEALTH_PREMIUM_CAP[year]


def ei_unemployment_rate(as_of) -> float:
    """실업급여 보험료율(노사 합계). 연중 개정이 있어 **시행일 기준**으로 고른다."""
    d = _d(as_of)
    rate = None
    for start, r in EI_UNEMPLOYMENT_STEPS:
        if d >= start:
            rate = r
    if rate is None:
        raise ValueError(
            f"{d} 시점의 실업급여 보험료율이 파라미터 표에 없습니다 "
            f"(표의 시작: {EI_UNEMPLOYMENT_STEPS[0][0]}).")
    return rate


def ei_stability_rate(scale: str) -> float:
    """고용안정·직업능력개발사업 보험료율 (사업주 전액). scale은 EI_STABILITY_RATES의 구분."""
    table = dict(EI_STABILITY_RATES)
    if scale not in table:
        raise ValueError(f"알 수 없는 사업 규모 구분 {scale!r} — 사용 가능: {', '.join(table)}")
    return table[scale]


# ---------------------------------------------------------------------------
# 보험료 계산
# ---------------------------------------------------------------------------

def _dec(x) -> Decimal:
    """금액·요율을 오차 없는 십진수로. float 연산을 그대로 절사하면 27,000원이 26,990원이 된다
    (3,000,000 × 0.018 ÷ 2 = 26999.999999999996). 보험료는 전부 이 타입으로 계산한다."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


# 절사 전에 털어낼 부동소수점 먼지의 한계. 보험료에 소수점 6자리 아래가 의미를 갖는
# 경우는 없고, 이보다 성기게 잘라내면 진짜 단수(26,999.99원)까지 올려 버린다.
_DUST = Decimal("0.000001")


def _floor10(x) -> int:
    """10원 미만 절사. 국민연금은 법 제117조(단수의 처리)→국고금관리법 준용,
    건강보험·장기요양·고용보험은 법률에 단수 규정이 없고 공단 실무가 원 단위 절사다.

    float로 계산된 값이 들어와도 맞게 자르도록 먼지를 먼저 턴다 — 27,000원이 되어야 할
    3,000,000 × 0.018 ÷ 2가 float에서는 26999.999999999996이라 그냥 절사하면 26,990원이 된다.
    """
    v = _dec(x).quantize(_DUST) if isinstance(x, float) else _dec(x)
    return int((v / 10).to_integral_value(rounding=ROUND_FLOOR) * 10)


def calc_social_insurance(monthly_wage: float, as_of: str = "", *,
                          scale: str = "150명 미만",
                          industrial_accident_rate: float = 0.0,
                          apply_caps: bool = True,
                          pension_income: float = 0.0) -> dict:
    """직장가입자 1명의 월 4대보험료를 계산한다.

    monthly_wage: 보수월액 (비과세 제외한 과세 보수 월액)
    as_of:        기준일 (기본 오늘). 요율·상한이 **연중에도 바뀌므로** 일자로 받는다.
    scale:        고용안정·직업능력개발사업 요율 구분 (EI_STABILITY_RATES)
    industrial_accident_rate: 산재보험료율 (업종별 고시값, 예: 0.0085). 0이면 산재는 계산하지 않는다.
    apply_caps:   건강보험료 상·하한, 연금 기준소득월액 상·하한 적용 여부
    pension_income: 연금 기준소득월액을 보수월액과 달리 잡을 때만 지정 (기본은 보수월액)

    반환은 프로젝트 공통 규약 {"결과", "계산과정", "근거", "주의사항"}.
    """
    if monthly_wage is None or float(monthly_wage) < 0:
        raise ValueError(f"보수월액은 0 이상이어야 합니다: {monthly_wage!r}")
    wage = _dec(monthly_wage)
    d = _d(as_of) if as_of else date.today()
    year = d.year
    steps, refs, warns = [], [], []

    # --- 건강보험 -----------------------------------------------------------
    h_rate = health_rate(year)
    h_total_raw = wage * _dec(h_rate)
    hi_cap, lo_cap = health_premium_cap(year) if apply_caps else (None, None)
    h_total = h_total_raw
    if apply_caps:
        if h_total_raw > hi_cap:
            h_total = _dec(hi_cap)
            steps.append(f"건강보험료 상한 적용: {h_total_raw:,.0f}원 → {hi_cap:,}원")
        elif h_total_raw < lo_cap:
            h_total = _dec(lo_cap)
            steps.append(f"건강보험료 하한 적용: {h_total_raw:,.0f}원 → {lo_cap:,}원")
    h_emp = _floor10(h_total / 2)
    h_er = _floor10(h_total / 2)
    steps.insert(0, f"건강보험료 = 보수월액 {wage:,.0f} × {_pct(h_rate)} = {h_total_raw:,.0f}원 (노사 합계)")
    steps.append(f"건강보험 근로자 {h_emp:,}원 · 사업주 {h_er:,}원 (각 50%, 10원 미만 절사)")
    refs.append(f"국민건강보험법 제73조·제76조①, 같은 법 시행령 제44조①({year}년 1만분의 {h_rate*10000:.0f})")
    if apply_caps:
        refs.append(f"보건복지부 고시 「월별 건강보험료액의 상한과 하한」({year}년 상한 {hi_cap:,}원·하한 {lo_cap:,}원)")

    # --- 장기요양보험 -------------------------------------------------------
    l_ratio = ltc_ratio(year)
    l_total = h_total * _dec(l_ratio)
    l_emp = _floor10(l_total / 2)
    l_er = _floor10(l_total / 2)
    if year >= LTC_BASE_CHANGED_YEAR:
        steps.append(f"장기요양보험료 = 건강보험료 {h_total:,.0f} × (장기요양요율 {_pct(ltc_rate(year))} "
                     f"÷ 건강보험료율 {_pct(h_rate)} = {_pct(l_ratio)}) = {l_total:,.0f}원")
        refs.append(f"노인장기요양보험법 제9조①, 같은 법 시행령 제4조({year}년 100만분의 {ltc_rate(year)*1_000_000:,.0f})")
    else:
        steps.append(f"장기요양보험료 = 건강보험료 {h_total:,.0f} × {_pct(l_ratio)} = {l_total:,.0f}원")
        refs.append(f"노인장기요양보험법 시행령 제4조({year}년 1만분의 {l_ratio*10000:,.0f} — 건강보험료 대비)")
    steps.append(f"장기요양 근로자 {l_emp:,}원 · 사업주 {l_er:,}원")

    # --- 국민연금 -----------------------------------------------------------
    p_rate = pension_rate(year)
    p_base_raw = _dec(pension_income) if pension_income else wage
    p_lo, p_hi, band_year = pension_income_band(d)
    p_base = p_base_raw
    if apply_caps:
        if p_base_raw > p_hi:
            p_base = _dec(p_hi)
            steps.append(f"연금 기준소득월액 상한 적용: {p_base_raw:,.0f}원 → {p_hi:,}원")
        elif p_base_raw < p_lo:
            p_base = _dec(p_lo)
            steps.append(f"연금 기준소득월액 하한 적용: {p_base_raw:,.0f}원 → {p_lo:,}원")
    p_emp = _floor10(p_base * _dec(p_rate))
    p_er = p_emp
    steps.append(f"국민연금 = 기준소득월액 {p_base:,.0f} × {_pct(p_rate)} = 근로자 {p_emp:,}원 · 사업주 {p_er:,}원")
    if 2026 <= year <= 2032:
        refs.append(f"국민연금법 부칙(법률 제20903호) 제4조① — {year}년 기여금·부담금 각 1만분의 {p_rate*10000:.0f}")
        warns.append(f"{year}년 국민연금 요율은 본칙(각 6.5%)이 아니라 부칙 특례 각 {_pct(p_rate)}입니다 — "
                     f"2032년까지 매년 0.25%p씩 올라 2033년에 6.5%가 됩니다.")
    else:
        refs.append(f"국민연금법 제88조③ — 기여금·부담금 각 1천분의 {p_rate*1000:.0f}")
    refs.append(f"보건복지부 고시 「국민연금 기준소득월액 하한액과 상한액」"
                f"({band_year}년 7월~{band_year+1}년 6월 하한 {p_lo:,}원·상한 {p_hi:,}원)")

    # --- 고용보험 -----------------------------------------------------------
    ei_u = ei_unemployment_rate(d)
    ei_emp = _floor10(wage * _dec(ei_u) / 2)
    ei_er_u = ei_emp
    ei_s = ei_stability_rate(scale)
    ei_er_s = _floor10(wage * _dec(ei_s))
    steps.append(f"고용보험 실업급여 = 보수월액 {wage:,.0f} × {_pct(ei_u)} → 근로자 {ei_emp:,}원 · 사업주 {ei_er_u:,}원 (각 1/2)")
    steps.append(f"고용안정·직업능력개발({scale}) = {wage:,.0f} × {_pct(ei_s)} = {ei_er_s:,}원 (사업주 전액)")
    refs.append(f"고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률 시행령 제12조① "
                f"— 실업급여 1천분의 {ei_u*1000:.0f}, 고용안정·직능 1만분의 {ei_s*10000:.0f}")

    # --- 산재보험 -----------------------------------------------------------
    ia_er = 0
    if industrial_accident_rate:
        ia_er = _floor10(wage * _dec(industrial_accident_rate))
        steps.append(f"산재보험 = {wage:,.0f} × {_pct(industrial_accident_rate)} = {ia_er:,}원 (사업주 전액)")
        refs.append("고용노동부 고시 「사업종류별 산재보험료율」 — 업종별 요율은 고시에서 확인해 인자로 넣은 값")
    else:
        warns.append("산재보험료는 업종별 요율이 필요해 계산하지 않았습니다 — "
                     "고용노동부 고시 「사업종류별 산재보험료율」에서 해당 업종 요율을 찾아 "
                     "industrial_accident_rate로 넣으면 함께 계산합니다.")

    emp_total = h_emp + l_emp + p_emp + ei_emp
    er_total = h_er + l_er + p_er + ei_er_u + ei_er_s + ia_er

    warns.append("건강보험·장기요양·고용보험은 법률에 단수 처리 규정이 없어 공단 실무(10원 미만 절사)를 "
                 "따랐습니다. 실제 고지액은 공단 부과 결과와 대조하세요.")
    warns.append("보수월액은 비과세를 뺀 과세 보수 기준입니다. 식대·차량유지비 등 비과세 항목을 "
                 "포함하면 보험료가 과대 계산됩니다.")
    warns.append("최종 판단은 공인노무사 확인이 필요합니다.")

    return {
        "결과": {
            "기준일": d.isoformat(), "연도": year, "보수월액": round(wage),
            "근로자부담": {
                "건강보험": h_emp, "장기요양": l_emp, "국민연금": p_emp,
                "고용보험": ei_emp, "합계": emp_total,
            },
            "사업주부담": {
                "건강보험": h_er, "장기요양": l_er, "국민연금": p_er,
                "고용보험_실업급여": ei_er_u, "고용보험_고용안정직능": ei_er_s,
                "산재보험": ia_er, "합계": er_total,
            },
            "노사합계": emp_total + er_total,
            "적용요율": {
                "건강보험": h_rate, "장기요양_건강보험료대비": l_ratio,
                "국민연금_각각": p_rate, "고용보험_실업급여": ei_u,
                "고용안정직능": ei_s, "산재보험": float(industrial_accident_rate or 0),
            },
        },
        "계산과정": steps, "근거": refs, "주의사항": warns,
    }


def check_premium_support(monthly_wage: float, employee_count: int, *,
                          as_of: str = "", newly_insured: bool = True,
                          property_tax_base: float = 0.0, global_income: float = 0.0,
                          supported_months: int = 0,
                          is_owner_or_ceo: bool = False) -> dict:
    """두루누리 사회보험료 지원(고용보험료·연금보험료) 해당 여부와 지원액을 판정한다.

    employee_count: 근로자인 피보험자 수 (월평균)
    newly_insured:  지원신청일 직전 1년간 해당 보험 가입 이력이 없는지 (신규가입 요건)
    property_tax_base / global_income: 0이면 '미확인'으로 두고 요건 충족으로 단정하지 않는다
    supported_months: 2018-01-01 이후 이미 지원받은 개월 수
    is_owner_or_ceo:  개인사업장 사용자·법인 대표이사면 연금보험료 지원 대상에서 빠진다
    """
    P = PREMIUM_SUPPORT
    wage = _dec(monthly_wage or 0)
    d = _d(as_of) if as_of else date.today()
    checks, warns = [], []

    ok_size = int(employee_count) < P["근로자수_미만"]
    ok_wage = wage < P["월보수_미만"]
    ok_new = bool(newly_insured)
    ok_months = int(supported_months) < P["지원기간_개월"]
    checks.append({"요건": f"근로자 {P['근로자수_미만']}명 미만", "값": f"{employee_count}명", "충족": ok_size})
    checks.append({"요건": f"월 보수 {P['월보수_미만']:,}원 미만", "값": f"{wage:,.0f}원", "충족": ok_wage})
    checks.append({"요건": "직전 1년간 가입 이력 없음(신규가입)", "값": "예" if ok_new else "아니오", "충족": ok_new})
    checks.append({"요건": f"누적 지원 {P['지원기간_개월']}개월 미만",
                   "값": f"{supported_months}개월", "충족": ok_months})

    if property_tax_base:
        ok_prop = float(property_tax_base) < P["재산과세표준_미만"]
        checks.append({"요건": f"재산 과세표준 {P['재산과세표준_미만']:,}원 미만",
                       "값": f"{float(property_tax_base):,.0f}원", "충족": ok_prop})
    else:
        ok_prop = None
        checks.append({"요건": f"재산 과세표준 {P['재산과세표준_미만']:,}원 미만", "값": "미확인", "충족": None})
        warns.append("재산 과세표준을 넣지 않아 그 요건은 판정하지 않았습니다 — 6억원 이상이면 지원 대상이 아닙니다.")
    if global_income:
        ok_inc = float(global_income) < P["종합소득_미만"]
        checks.append({"요건": f"종합소득 {P['종합소득_미만']:,}원 미만",
                       "값": f"{float(global_income):,.0f}원", "충족": ok_inc})
    else:
        ok_inc = None
        checks.append({"요건": f"종합소득 {P['종합소득_미만']:,}원 미만", "값": "미확인", "충족": None})
        warns.append("종합소득을 넣지 않아 그 요건은 판정하지 않았습니다 — 연 4,300만원 이상이면 지원 대상이 아닙니다.")

    hard = [ok_size, ok_wage, ok_new, ok_months]
    soft = [c for c in (ok_prop, ok_inc) if c is not None]
    rejected = not (all(hard) and all(soft))       # 확인된 요건 중 하나라도 어긋나면 탈락
    unknown = (ok_prop is None or ok_inc is None)  # 재산·소득을 안 줘서 확인 못 한 요건이 있다
    eligible = not rejected and not unknown        # '대상'은 **전 요건을 확인했을 때만**

    # 지원액 — 각각 80%, 상한 적용
    ei_u = ei_unemployment_rate(d)
    ei_emp_prem = _floor10(wage * _dec(ei_u) / 2)
    ei_er_prem = ei_emp_prem + _floor10(wage * _dec(ei_stability_rate("150명 미만")))
    p_rate = pension_rate(d.year)
    p_lo, p_hi, _ = pension_income_band(d)
    p_base = min(max(wage, _dec(p_lo)), _dec(p_hi))
    p_prem = _floor10(p_base * _dec(p_rate))

    def _sup(prem, cap):
        return min(_floor10(_dec(prem) * _dec(P["지원율"])), cap)

    ei_sup_emp = _sup(ei_emp_prem, P["고용보험_월한도_근로자"])
    ei_sup_er = _sup(ei_er_prem, P["고용보험_월한도_사업주"])
    p_sup_emp = 0 if is_owner_or_ceo else _sup(p_prem, P["연금_월한도"])
    p_sup_er = 0 if is_owner_or_ceo else _sup(p_prem, P["연금_월한도"])
    if is_owner_or_ceo:
        warns.append("개인사업장 사용자·법인 대표이사는 연금보험료 지원 대상에서 제외됩니다 "
                     "(소규모사업장 저소득근로자 연금보험료 지원 고시 제1조). 고용보험료 지원은 별개로 판단하세요.")

    # 지원액은 '확정 대상'과 '판정보류' 모두에 보여 준다 — 보류는 확인만 더 하면 되는
    # 상태라 금액이 곧 쓸모 있고, 확정 탈락일 때만 0으로 닫는다.
    show_amount = not rejected
    total = (ei_sup_emp + ei_sup_er + p_sup_emp + p_sup_er) if show_amount else 0
    warns.append("건강보험료·장기요양보험료는 두루누리 지원 대상이 아닙니다 — 근거 법률이 없습니다.")
    warns.append("예산 범위 내 지원이라 예산 소진 시 중단될 수 있고, 실제 지원 여부는 "
                 "근로복지공단·국민연금공단의 결정에 따릅니다.")

    return {
        "결과": {
            "기준일": d.isoformat(),
            "지원대상": None if (unknown and not rejected) else eligible,
            "판정": ("대상 아님" if rejected else
                   ("판정보류(재산·소득 미확인)" if unknown else "대상")),
            "요건점검": checks,
            "월지원액": {
                "고용보험_근로자분": ei_sup_emp, "고용보험_사업주분": ei_sup_er,
                "연금_근로자분": p_sup_emp, "연금_사업주분": p_sup_er, "합계": total,
                **({"단서": "재산·종합소득 요건이 미확인이라 **잠정액**입니다 — 두 값을 넣어 다시 확인하세요."}
                   if unknown else {}),
            } if show_amount else {"합계": 0},
            "남은지원개월": max(0, P["지원기간_개월"] - int(supported_months)),
        },
        "계산과정": [
            f"고용보험료 근로자분 {ei_emp_prem:,}원 × 80% → 지원 {ei_sup_emp:,}원 (월 한도 {P['고용보험_월한도_근로자']:,}원)",
            f"고용보험료 사업주분 {ei_er_prem:,}원 × 80% → 지원 {ei_sup_er:,}원 (월 한도 {P['고용보험_월한도_사업주']:,}원)",
            f"연금보험료 각 {p_prem:,}원 × 80% → 지원 각 {p_sup_emp:,}원 (월 한도 {P['연금_월한도']:,}원)",
        ],
        "근거": [
            "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률 제21조, 같은 법 시행령 제28조",
            "고용노동부 고시 「고용보험료의 지원 대상 및 지원 수준 등에 관한 고시」 제4조~제6조",
            "국민연금법 제100조의3, 같은 법 시행령 제73조의2·제73조의3",
            "보건복지부 고시 「소규모사업장 저소득근로자에 대한 연금보험료 지원 등에 관한 고시」 제2조~제5조",
        ],
        "주의사항": warns,
    }


# ---------------------------------------------------------------------------
# 표 검증 — 하드코딩한 요율이 현행 법령과 같은지 확인 (check_sources_health에서 호출)
# ---------------------------------------------------------------------------

def verify_rates(year: int = 0) -> dict:
    """현행 법령 조문을 읽어 이 파일의 요율표와 대조한다. 네트워크가 필요하다.

    해마다 요율이 바뀌는데 표만 남고 갱신을 잊으면 모든 계산이 조용히 틀린다 —
    그것을 알아채기 위한 점검이다. 조회 실패는 '불일치'가 아니라 '확인 못 함'이다.
    """
    import re
    year = int(year) or date.today().year
    out = {"연도": year, "항목": []}

    def _row(name, expected, got, ref):
        ok = (got is not None) and abs(got - expected) < 1e-9
        out["항목"].append({"항목": name, "표의 값": expected, "법령 값": got,
                           "일치": ok if got is not None else None, "근거": ref})

    try:
        from law_go_kr import LawGoKrClient
        c = LawGoKrClient()
    except Exception as e:                                     # noqa: BLE001
        out["상태"] = f"확인 못 함 — law.go.kr 클라이언트 로드 실패: {type(e).__name__}"
        return out

    def _article(law_name, art_no):
        rows = c.search_laws(law_name, display=5)
        hit = next((r for r in rows if (r.get("법령명") or "").strip() == law_name), rows[0])
        return re.sub(r"\s+", " ", c.law_article(hit["MST"], art_no).get("원문", ""))

    try:
        t = _article("국민건강보험법 시행령", "44")
        m = re.search(r"1만분의\s*([\d,]+)", t)
        _row("건강보험료율", HEALTH_RATE.get(year), int(m.group(1).replace(",", "")) / 10000 if m else None,
             "국민건강보험법 시행령 제44조①")
    except Exception as e:                                     # noqa: BLE001
        _row("건강보험료율", HEALTH_RATE.get(year), None, f"조회 실패: {type(e).__name__}")

    try:
        t = _article("노인장기요양보험법 시행령", "4")
        m = re.search(r"100만분의\s*([\d,]+)", t)
        _row("장기요양보험료율", LTC_RATE.get(year),
             int(m.group(1).replace(",", "")) / 1_000_000 if m else None,
             "노인장기요양보험법 시행령 제4조")
    except Exception as e:                                     # noqa: BLE001
        _row("장기요양보험료율", LTC_RATE.get(year), None, f"조회 실패: {type(e).__name__}")

    try:
        t = _article("고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률 시행령", "12")
        m = re.search(r"실업급여의 보험료율\s*[:：]?\s*1천분의\s*([\d,]+)", t)
        _row("고용보험 실업급여 요율", ei_unemployment_rate(date(year, 12, 31)),
             int(m.group(1).replace(",", "")) / 1000 if m else None,
             "징수법 시행령 제12조①2호")
    except Exception as e:                                     # noqa: BLE001
        _row("고용보험 실업급여 요율", None, None, f"조회 실패: {type(e).__name__}")

    checked = [i for i in out["항목"] if i["일치"] is not None]
    bad = [i for i in checked if not i["일치"]]
    out["상태"] = ("OK" if checked and not bad else
                   ("불일치 — 요율표를 갱신하세요" if bad else "확인 못 함 — 원천 조회 실패"))
    return out
