# -*- coding: utf-8 -*-
"""
tests/test_payroll.py — 임금대장 분석기·급여테이블 설계기 골든 시나리오

각 케이스는 실무에서 위반이 은폐·오검출되기 쉬운 패턴을 고정한다:
격월 상여 최저임금 은폐, 재직조건부 상여 통상임금(신법리), 주별 데이터 유무에 따른
주52h 판정 등급, 5인 미만·4인 이하 예외의 오검출 방지, 설계기의 순환 분해 정합성.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payroll import analyze_payroll, design_pay_table


def item(명칭, 금액, 주기="매월", 성격="기본급", 실비=False, 최소보장=None):
    d = {"명칭": 명칭, "금액": 금액, "지급주기": 주기, "성격": 성격, "실비변상": 실비}
    if 최소보장 is not None:
        d["최소보장액"] = 최소보장
    return d


def month(연월, 임금항목, 근로일수=20, 총근로시간=209.0, 연장=0.0, 야간=0.0, 휴일=0.0,
          주별=None, 공제=None):
    return {"연월": 연월, "임금항목": 임금항목, "근로일수": 근로일수,
            "총근로시간": 총근로시간, "연장시간": 연장, "야간시간": 야간,
            "휴일시간": 휴일, "주별근로시간": 주별, "공제내역": 공제}


def emp(월별, 사원ID="E1", 입사일="2024-01-01", 퇴사일=None, 주소정=40.0,
        수습=False, 계약1년이상=True, 단순노무=False, 감시단속=False, 성명=None):
    return {"사원ID": 사원ID, "성명": 성명, "입사일": 입사일, "퇴사일": 퇴사일,
            "주소정근로시간": 주소정, "수습여부": 수습, "계약기간1년이상": 계약1년이상,
            "단순노무직": 단순노무, "감시단속승인": 감시단속, "월별": 월별}


def payroll_data(직원, n=10, 연도=2026, flex="없음"):
    return {"사업장": {"상시근로자수": n, "연도": 연도, "탄력선택근로제": flex},
            "직원": 직원}


def entries_of(res, 검사항목, 사원ID=None):
    out = []
    for e in res["직원별"]:
        if 사원ID is not None and e["사원ID"] != 사원ID:
            continue
        out += [t for t in e["월별판정"] if t["검사항목"] == 검사항목]
    return out


# ---------------------------------------------------------------------------
# 골든 1: 격월 상여로 최저임금 위반 은폐 → 검출
# ---------------------------------------------------------------------------

class TestMinWageConcealment:
    def test_격월상여는_불산입이라_위반_검출(self):
        # 표면 월 수령액 190만+60만(격월)이지만 격월 상여는 최저임금 불산입 →
        # 산입액 190만 < 2026년 월 환산 최저임금 2,156,880원
        data = payroll_data([emp([month("2026-01", [
            item("기본급", 1_900_000),
            item("상여금", 600_000, "격월", "정기상여"),
        ])])])
        res = analyze_payroll(data)
        ents = entries_of(res, "최저임금")
        assert len(ents) == 1
        e = ents[0]
        assert e["판정"] == "위반"
        assert e["상세"]["부족액"] == pytest.approx(256_880)  # 2,156,880 − 1,900,000
        row = [d for d in e["상세"]["산입내역"] if d["명칭"] == "상여금"][0]
        assert row["판정"].startswith("불산입")
        assert res["요약"]["위반건수"]["최저임금"] == 1
        assert res["요약"]["총부족액추정"] == pytest.approx(256_880)

    def test_체불_1건에는_상습체불_제재_경고를_띄우지_않는다(self):
        """근기법 §43의4① 상습체불사업주 = 직전 1년 3개월분 이상 체불,
        또는 5회 이상 + 총액 3천만원 이상. 1건·소액에 제재 경고를 붙이면 경고가
        신뢰를 잃는다 (2026-09-01 리뷰)."""
        data = payroll_data([emp([month("2026-01", [item("기본급", 1_900_000)])])])
        res = analyze_payroll(data)
        assert not any("2025-10-23" in w for w in res["경고"])
        assert any("시정이 필요" in w for w in res["경고"])

    def test_3개월_이상_체불이면_상습체불_요건_안내(self):
        data = payroll_data([emp([month(ym, [item("기본급", 1_900_000)])
                                  for ym in ("2026-01", "2026-02", "2026-03")])])
        res = analyze_payroll(data)
        assert res["요약"]["체불월수"] == 3
        assert any("상습체불사업주" in w and "2025-10-23" in w for w in res["경고"])

    def test_적법_대장은_위반_0건(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)])])])
        res = analyze_payroll(data)
        assert res["요약"]["위반합계"] == 0
        assert res["요약"]["총부족액추정"] == 0


# ---------------------------------------------------------------------------
# 골든 2: 재직조건부 매월 상여 → 통상임금 포함 (2024-12-19 전합 신법리)
# ---------------------------------------------------------------------------

class TestOrdinaryWageBonus:
    def test_재직조건부_매월상여_통상임금_포함(self):
        # 스키마에 재직조건 필드 자체가 없음 — 신법리상 조건 무관 포함이 근거
        data = payroll_data([emp([month("2026-01", [
            item("기본급", 2_000_000),
            item("재직조건부상여", 300_000, "매월", "정기상여"),
        ])])])
        res = analyze_payroll(data)
        e = entries_of(res, "통상임금")[0]
        assert e["판정"] == "적법"
        assert e["상세"]["월통상임금"] == 2_300_000
        row = [r for r in e["상세"]["항목판정"] if r["명칭"] == "재직조건부상여"][0]
        assert row["통상임금"] == "포함"

    def test_가족수당은_불확실_판정으로_분리(self):
        data = payroll_data([emp([month("2026-01", [
            item("기본급", 2_300_000),
            item("가족수당", 50_000, 성격="가족수당"),
        ])])])
        res = analyze_payroll(data)
        e = entries_of(res, "통상임금")[0]
        assert e["판정"] == "불확실"
        assert "가족수당" in e["상세"]["불확실항목"]


# ---------------------------------------------------------------------------
# 골든 3: 주52시간 — 주별 데이터 있으면 확정, 없으면 추정(불확실)
# ---------------------------------------------------------------------------

class Test52Hours:
    def test_주별_54시간_주_위반_검출(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        연장=14.0, 주별=[40, 54, 40, 40])])])
        res = analyze_payroll(data)
        e = entries_of(res, "주52시간")[0]
        assert e["판정"] == "위반"
        assert e["상세"]["위반주차"] == [2]

    def test_주별없이_월연장_80시간은_불확실(self):
        # 주 평균 18.4h > 12h — 위반 강력 추정이지만 주별 데이터 없이는 불확실로만
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        연장=80.0)])])
        res = analyze_payroll(data)
        e = entries_of(res, "주52시간")[0]
        assert e["판정"] == "불확실"
        text = e["상세"]["요지"] + " ".join(e["상세"]["주의사항"])
        assert "주별" in text

    def test_주별없이_월연장_20시간은_불확실(self):
        """월 합계로는 '4주에 13시간씩 몰아 쓴 52시간'(주 단위 §53① 위반)을
        구분할 수 없다 — 한도 이내여도 적법으로 확정하지 않는다."""
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        연장=20.0)])])
        res = analyze_payroll(data)
        e = entries_of(res, "주52시간")[0]
        assert e["판정"] == "불확실"
        assert "주별" in e["상세"]["요지"] + " ".join(e["상세"]["주의사항"])

    def test_탄력근로제_특정주_초과는_불확실(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        주별=[40, 54, 40, 40])])], flex="탄력")
        res = analyze_payroll(data)
        assert entries_of(res, "주52시간")[0]["판정"] == "불확실"


# ---------------------------------------------------------------------------
# 골든 4: 5인 미만 사업장 — 가산·연차·근로시간한도 오검출 방지
# ---------------------------------------------------------------------------

class TestUnder5:
    def _res(self):
        직원 = [
            emp([month("2026-01", [item("기본급", 2_299_000)], 연장=20.0)], 사원ID="A"),
            emp([month("2026-01", [item("기본급", 2_299_000)],
                       총근로시간=None, 연장=None, 야간=None, 휴일=None)], 사원ID="B"),
        ]
        return analyze_payroll(payroll_data(직원, n=4))

    def test_연장_실지급_0원이면_가산은_미적용이되_시간분_임금은_불확실(self):
        """§56 가산 의무는 없지만 §43(근로 제공분 임금)은 5인 미만에도 적용된다.
        예전에는 연장 20시간분 임금이 한 푼도 없어도 '적법'으로 나갔다 (2026-09-01 리뷰)."""
        res = self._res()
        e = entries_of(res, "가산수당", 사원ID="A")[0]
        assert e["판정"] == "불확실"
        assert "§56" in e["상세"]["요지"]
        assert e["상세"]["시간분임금추정"] > 0
        assert res["요약"]["위반건수"].get("가산수당", 0) == 0

    def test_연차_검사는_미수행(self):
        assert entries_of(self._res(), "연차") == []

    def test_주52시간은_미적용_적법(self):
        e = entries_of(self._res(), "주52시간", 사원ID="A")[0]
        assert e["판정"] == "적법"
        assert "미적용" in e["상세"]["요지"]

    def test_4인이하_근로시간_null은_기재누락_아님(self):
        e = entries_of(self._res(), "기재사항", 사원ID="B")[0]
        assert e["판정"] == "적법"
        assert any("7호" in s for s in e["상세"]["생략적법"])

    def test_경고에_미적용_안내(self):
        assert any("4인" in w or "5인" in w for w in self._res()["경고"])


# ---------------------------------------------------------------------------
# 골든 5: 기재사항 누락 — 5인 이상 근로시간 null은 누락 플래그
# ---------------------------------------------------------------------------

class TestLedgerFields:
    def test_5인이상_근로시간_null은_누락_위반(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        총근로시간=None, 연장=None, 야간=None, 휴일=None)])],
                            n=10)
        res = analyze_payroll(data)
        e = entries_of(res, "기재사항")[0]
        assert e["판정"] == "위반"
        assert any("7호" in s for s in e["상세"]["누락호"])
        assert any("8호" in s for s in e["상세"]["누락호"])

    def test_감시단속_승인자는_근로시간_생략_적법(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        총근로시간=None, 연장=None, 야간=None, 휴일=None)],
                                 감시단속=True)], n=10)
        res = analyze_payroll(data)
        assert entries_of(res, "기재사항")[0]["판정"] == "적법"

    def test_근로일수_null은_예외없이_누락(self):
        # 시행령 §27③ 예외는 7·8호(근로시간류)만 — 6호 근로일수는 4인 이하도 기재 의무
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        근로일수=None, 총근로시간=None,
                                        연장=None, 야간=None, 휴일=None)])], n=4)
        res = analyze_payroll(data)
        e = entries_of(res, "기재사항")[0]
        assert e["판정"] == "위반"
        assert any("6호" in s for s in e["상세"]["누락호"])


# ---------------------------------------------------------------------------
# 골든 6: 가산수당 미지급 — 연장 20h 실지급 0원 검출 + 부족액
# ---------------------------------------------------------------------------

class TestOvertimeUnderpaid:
    def test_연장_20시간_실지급_0원_위반_부족액(self):
        # 기본급 2,299,000 → 통상시급 11,000원, 이론치 20×11,000×1.5 = 330,000원
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        연장=20.0)])], n=10)
        res = analyze_payroll(data)
        e = entries_of(res, "가산수당")[0]
        assert e["판정"] == "위반"
        assert e["상세"]["부족액"] == pytest.approx(330_000)
        assert res["요약"]["위반건수"]["가산수당"] == 1
        assert res["요약"]["총부족액추정"] == pytest.approx(330_000)

    def test_이론치만큼_지급하면_적법(self):
        data = payroll_data([emp([month("2026-01", [
            item("기본급", 2_299_000),
            item("연장수당", 330_000, 성격="연장수당"),
        ], 연장=20.0)])], n=10)
        res = analyze_payroll(data)
        assert entries_of(res, "가산수당")[0]["판정"] == "적법"

    def test_시간_기재없으면_불확실(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)],
                                        연장=None, 야간=None, 휴일=None)])], n=10)
        res = analyze_payroll(data)
        assert entries_of(res, "가산수당")[0]["판정"] == "불확실"


# ---------------------------------------------------------------------------
# 평균임금 — 퇴사자 3개월 창구 + 연간 상여 3/12 + 통상임금 하한
# ---------------------------------------------------------------------------

class TestAverageWageRetiree:
    def test_퇴사자_평균임금_상여_3_12_산입과_하한(self):
        months = []
        for m in range(1, 7):
            items = [item("기본급", 3_000_000)]
            if m in (3, 6):  # 분기 상여 — 창구 직접 산입 아닌 연간 3/12 채널
                items.append(item("분기상여", 600_000, "분기", "정기상여"))
            months.append(month(f"2026-{m:02d}", items))
        data = payroll_data([emp(months, 입사일="2024-07-01", 퇴사일="2026-07-01")])
        res = analyze_payroll(data)
        ents = entries_of(res, "평균임금")
        assert len(ents) == 1
        e = ents[0]
        assert e["판정"] == "적법"
        d = e["상세"]
        assert d["산정창구"]["대상월"] == ["2026-04", "2026-05", "2026-06"]
        assert d["산정창구"]["총일수"] == 91
        assert d["연간상여합계"] == 1_200_000
        assert d["상여산입액"] == pytest.approx(300_000)   # 1,200,000 × 3/12
        assert d["임금총액"] == pytest.approx(9_300_000)   # 창구 900만 + 30만
        assert d["1일평균임금"] == pytest.approx(9_300_000 / 91, abs=0.01)
        assert d["통상임금하한적용"] is True  # 월급제 40h — 1일 통상임금이 더 큼

    def test_재직자는_평균임금_검사_없음(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 3_000_000)])])])
        assert entries_of(analyze_payroll(data), "평균임금") == []

    def test_창구_데이터_부족하면_불확실(self):
        data = payroll_data([emp([month("2026-06", [item("기본급", 3_000_000)])],
                                 입사일="2026-06-01", 퇴사일="2026-07-01")])
        res = analyze_payroll(data)
        assert entries_of(res, "평균임금")[0]["판정"] == "불확실"


# ---------------------------------------------------------------------------
# 연차 발생 정보 — 5인 이상만 수행
# ---------------------------------------------------------------------------

class TestAnnualLeaveInfo:
    def test_5인이상은_연차_발생정보_제공(self):
        data = payroll_data([emp([month("2026-01", [item("기본급", 2_299_000)])],
                                 입사일="2025-01-01")], n=10)
        res = analyze_payroll(data)
        ents = entries_of(res, "연차")
        assert len(ents) == 1
        assert ents[0]["연월"] is None  # 직원 단위 검사
        # 2025-01-01 입사, 기준 2026-01-31: 월개근 11 + 1년차 15 = 26
        assert ents[0]["상세"]["총발생일수"] == 26


# ---------------------------------------------------------------------------
# design_pay_table — 역산 분해·재합산 정합 + 최저임금 역검증
# ---------------------------------------------------------------------------

class TestDesignPayTable:
    def test_월총액300만_고정OT20h_분해_재합산_일치(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "월총액", "금액": 3_000_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        t = r["급여테이블"]
        # X = 3,000,000 × 209 ÷ (209+30) — 통상시급 순환의 대수적 해
        assert t["기본급"] == pytest.approx(2_623_430.96, abs=0.05)
        assert t["통상시급"] == pytest.approx(12_552.30, abs=0.01)
        assert t["고정연장수당"] == pytest.approx(t["통상시급"] * 20 * 1.5, abs=1)
        assert t["월지급총액"] == pytest.approx(3_000_000, abs=0.05)
        assert r["검증"]["최저임금판정"] == "적법"

    def test_월총액210만_고정OT30h는_최저임금_위반_경고(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 30, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "월총액", "금액": 2_100_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        assert r["검증"]["최저임금판정"] == "위반"
        assert r["검증"]["상세"]["위반여부"] is True
        assert any("최저임금" in c for c in r["주의사항"])

    def test_수당_상여_포함_재구성_정합(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "월총액", "금액": 3_000_000},
            "고정수당": [item("식대", 200_000, 성격="식대")],
            "정기상여": {"연간총액": 2_400_000, "지급주기": "분기"},
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        t = r["급여테이블"]
        # 분기 상여도 통상임금 산입(신법리) — 월총액에는 미포함, 연봉환산에는 포함
        assert t["월통상임금"] == pytest.approx(t["기본급"] + 200_000 + 200_000, abs=0.05)
        assert t["통상시급"] == pytest.approx(t["월통상임금"] / 209, abs=0.01)
        assert t["월지급총액"] == pytest.approx(3_000_000, abs=0.05)
        assert t["연봉환산"] == pytest.approx(3_000_000 * 12 + 2_400_000, abs=1)

    def test_연봉방식은_월총액과_동일_분해(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "연봉", "금액": 36_000_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        assert r["급여테이블"]["기본급"] == pytest.approx(2_623_430.96, abs=0.05)

    def test_기본급방식_전개(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 10, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "기본급", "금액": 2_299_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        t = r["급여테이블"]
        assert t["통상시급"] == pytest.approx(11_000, abs=0.01)
        assert t["고정연장수당"] == pytest.approx(165_000, abs=0.1)  # 11,000×10×1.5
        assert t["월지급총액"] == pytest.approx(2_464_000, abs=0.1)
        assert r["검증"]["최저임금판정"] == "적법"

    def test_5인미만은_가산없는_단가로_설계(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 4, "주소정근로시간": 40,
            "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "월총액", "금액": 3_000_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        t = r["급여테이블"]
        assert t["가산배율"]["연장"] == 1.0
        assert t["고정연장수당"] == pytest.approx(t["통상시급"] * 20, abs=1)
        assert t["월지급총액"] == pytest.approx(3_000_000, abs=0.05)
        assert any("5인 미만" in c for c in r["주의사항"])

    def test_수습_감액_반영_검증(self):
        # 기본급 2,000,000: 정상이면 미달(비교 9,569 < 10,320)이지만
        # 수습 3요건 충족 시 감액 시급 9,288원 기준으로는 적법
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 0, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "기본급", "금액": 2_000_000},
            "고정수당": [], "정기상여": None,
            "수습적용": True, "계약기간1년이상": True, "단순노무직": False,
        })
        assert r["검증"]["상세"]["수습감액적용"] is True
        assert r["검증"]["최저임금판정"] == "적법"

    def test_계약서_문안_필수요소(self):
        r = design_pay_table({
            "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
            "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
            "목표": {"방식": "월총액", "금액": 3_000_000},
            "고정수당": [], "정기상여": None,
            "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
        })
        문안 = r["계약서_임금조항_문안"]
        assert "20시간" in 문안          # 고정OT 시간 수 명시 (§17② 계산방법)
        assert "계산방법" in 문안
        assert "지급방법" in 문안
        assert "기본급" in 문안
        # 통상임금 절감 설계 실익 없음 주의 상시 포함
        assert any("2020다247190" in c or "전원합의체" in c for c in r["주의사항"])

    def test_설계불능이면_ValueError(self):
        with pytest.raises(ValueError):
            design_pay_table({
                "연도": 2026, "상시근로자수": 10, "주소정근로시간": 40,
                "고정연장시간_월": 20, "고정야간시간_월": 0, "고정휴일시간_월": 0,
                "목표": {"방식": "월총액", "금액": 100_000},
                "고정수당": [item("식대", 500_000, 성격="식대")], "정기상여": None,
                "수습적용": False, "계약기간1년이상": True, "단순노무직": False,
            })

    def test_잘못된_방식은_ValueError(self):
        with pytest.raises(ValueError):
            design_pay_table({"연도": 2026, "목표": {"방식": "시급", "금액": 10_000}})
