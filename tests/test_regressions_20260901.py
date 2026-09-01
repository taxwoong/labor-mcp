# -*- coding: utf-8 -*-
"""2026-09-01 다중 에이전트 코드리뷰에서 확인된 실버그의 회귀 테스트.

각 테스트는 "예전에는 무엇이 잘못 나갔는지"를 docstring에 남긴다 — 같은 실수가
다시 들어오면 여기서 걸린다. 네트워크 불필요(law.go.kr 응답은 문자열로 흉내).
"""
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import calculators as calc  # noqa: E402
import labor_constants as lc  # noqa: E402
import law_go_kr as L  # noqa: E402
import payroll as pr  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 임금항목 분류 — 최관대 버킷으로 새던 경로
# ---------------------------------------------------------------------------

class Test임금항목분류:
    def test_스키마에_없는_성격은_기타로_강등된다(self):
        """예전: 성격='상여금'(미지 값)이 else로 떨어져 **전액 산입** →
        최저임금 위반이 '적법'으로 뒤집혔다. '정기상여'면 부분산입인데."""
        c = calc.classify_wage_item({"명칭": "수당", "금액": 300000, "성격": "상여금"}, 2026)
        assert c["최저임금산입"].startswith("불확실")
        assert any("스키마에 없는 값" in g for g in c["근거"])

    def test_기타는_3축_모두_불확실(self):
        """리소스 문서(임금항목_산입매트릭스 §3)가 '기타는 3축 모두 △ 불확실'이라고
        약속하는데, 예전 코드는 통상임금 축만 불확실이고 최저임금은 산입·평균임금은 포함이었다."""
        c = calc.classify_wage_item({"명칭": "??수당", "금액": 500000, "성격": "기타"}, 2026)
        assert c["최저임금산입"].startswith("불확실")
        assert c["통상임금"].startswith("불확실")
        assert c["평균임금"].startswith("불확실")

    def test_매월_지급_성과급을_불산입으로_확정하지_않는다(self):
        """예전: 성과급이면 주기 불문 최저임금 불산입 → 없는 위반을 만들어냈다.
        최저임금법 §6④ 불산입 목록(시행규칙 §2)에 성과급은 없다."""
        c = calc.classify_wage_item(
            {"명칭": "성과수당", "금액": 300000, "성격": "성과급", "지급주기": "매월"}, 2026)
        assert not c["최저임금산입"].startswith("불산입")

    def test_현물_지급은_최저임금_불산입(self):
        """최저임금법 §6④3호가목(통화 이외의 것). 예전에는 구분 필드 자체가 없어
        현물 식대가 전액 산입되며 **위반을 놓쳤다**."""
        c = calc.classify_wage_item(
            {"명칭": "현물식대", "금액": 200000, "성격": "식대", "현물": True}, 2026)
        assert c["최저임금산입"].startswith("불산입")

    def test_성격_생략시_명칭이_성격이면_인정(self):
        """{'명칭':'기본급'}처럼 성격을 생략한 입력이 전부 불확실로 빠지면 안 된다."""
        c = calc.classify_wage_item({"명칭": "기본급", "금액": 2000000}, 2026)
        assert c["최저임금산입"] == "산입"


# ---------------------------------------------------------------------------
# 2. 최저임금 — 3단계 판정과 분모
# ---------------------------------------------------------------------------

class Test최저임금:
    def test_불확실_항목이_결론을_가르면_불확실로_남는다(self):
        r = calc.check_minimum_wage(
            [{"명칭": "기본급", "금액": 1_900_000},
             {"명칭": "성과수당", "금액": 300_000, "성격": "성과급", "지급주기": "매월"}],
            40, 2026)["결과"]
        assert r["판정"] == "불확실"
        assert r["비교시급"] < r["적용최저시급"] <= r["비교시급_불확실포함"]

    def test_주_15시간_미만은_주휴시간을_더하지_않는다(self):
        """근기법 §18③(§55·§60 적용 제외) + 최저임금법 시행령 §5①.
        예전: 무조건 가산해 주 12h 근로자의 분모가 62.6h → **허위 위반**."""
        assert lc.monthly_standard_hours(12) == pytest.approx(52.1, abs=0.1)
        assert lc.monthly_standard_hours(40) == 209.0
        r = calc.check_minimum_wage([{"명칭": "기본급", "금액": 600_000}], 12, 2026)["결과"]
        assert r["판정"] == "적법"

    def test_주소정근로시간_0은_거부한다(self):
        """예전: 위반 True + 월부족액 -2,000,000 같은 모순된 값이 나갔다."""
        with pytest.raises(ValueError):
            calc.check_minimum_wage([{"명칭": "기본급", "금액": 2_000_000}], 0, 2026)


# ---------------------------------------------------------------------------
# 3. 가산수당·퇴직급여·연차
# ---------------------------------------------------------------------------

class Test계산기:
    def test_휴일_8시간_초과분_이중계상_금지(self):
        """holiday_h=10, over8=2 → 예전 190,000원(총시간에 1.5배 후 초과분 가산).
        정답은 8×1.5×10,000 + 2×2.0×10,000 = 160,000원 (§56② 1·2호)."""
        r = calc.calc_overtime_pay(10000, 0, 0, holiday_h=10, holiday_over8_h=2, employees=5)
        assert r["결과"]["합계"] == pytest.approx(160_000)
        auto = calc.calc_overtime_pay(10000, 0, 0, holiday_h=10, employees=5)
        assert auto["결과"]["합계"] == pytest.approx(160_000)

    def test_음수_시간은_거부한다(self):
        with pytest.raises(ValueError):
            calc.calc_overtime_pay(10000, -100, 0, 0)

    @pytest.mark.parametrize("plan", ["DC", "dc", "DC형", "확정기여형(DC)", "확정기여"])
    def test_DC형_표기_변형에도_퇴직금_공식을_쓰지_않는다(self, plan):
        """예전: endswith('DC')만 봐서 'DC형'이면 평균임금 방식 금액(12,328,767원)을
        확신에 차서 반환했다."""
        r = calc.calc_severance_pay(150000, 1000, 40, plan_type=plan)["결과"]
        assert r["제도"] == "DC"
        assert r["퇴직급여"] is None

    def test_월말_입사자의_1년_만료일은_다음달_1일(self):
        """2024-02-29 + 12개월을 2025-02-28로 당기면 연차 15일이 하루 일찍 발생한다."""
        assert calc._add_months(date(2024, 2, 29), 12) == date(2025, 3, 1)
        assert calc._add_months(date(2024, 1, 31), 1) == date(2024, 3, 1)
        assert calc._add_months(date(2024, 3, 15), 12) == date(2025, 3, 15)

    def test_출근율_80퍼센트_미만이면_월단위_연차를_안내한다(self):
        """근기법 §60② — 80% 미만 출근자도 1개월 개근 시 1일. 예전에는 0일로 확정해
        법정 최소치를 밑도는 값을 그대로 내보냈다."""
        r = calc.calc_annual_leave("2022-01-01", "2024-01-01", attendance_rate=0.5)
        assert any("§60②" in c for c in r["주의사항"])

    def test_연차_총발생일수가_누적임을_고지한다(self):
        r = calc.calc_annual_leave("2010-01-01", "2026-01-01")
        assert any("누적 발생분" in c for c in r["주의사항"])


# ---------------------------------------------------------------------------
# 4. law.go.kr 클라이언트 — 장애가 '자료 없음'으로 둔갑하던 경로
# ---------------------------------------------------------------------------

class TestLawGoKr:
    def test_인증_실패_봉투를_감지한다(self):
        """실측 응답: HTTP 200 + <result>사용자 정보 검증에 실패하였습니다.</result>
        본문에 '인증'이라는 단어가 없어 예전 가드('인증' and '실패')는 걸리지 않았고,
        이 XML이 정상 파싱돼 모든 도구가 조용히 0건을 반환했다."""
        xml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
               '<result>사용자 정보 검증에 실패하였습니다.</result>'
               '<msg>정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요.</msg></Response>')
        with pytest.raises(L.LawAuthError):
            L._check_auth_error(xml)

    def test_정상_응답은_인증오류로_오판하지_않는다(self):
        L._check_auth_error('<?xml version="1.0"?><PrecSearch><totalCnt>531</totalCnt></PrecSearch>')

    @pytest.mark.parametrize("raw,want", [
        ("20250315", "20250315"), ("2025-03-15", "20250315"), ("2025.3.5", "20250305"),
    ])
    def test_날짜_정규화(self, raw, want):
        assert L._norm_date(raw) == want

    @pytest.mark.parametrize("raw", ["2025", "20251345", "abc"])
    def test_잘못된_날짜는_거부한다(self, raw):
        """예전: 문자열 비교라 '2025-03-15'가 오류 없이 **다른 시행본**(20241022)을 골랐다."""
        with pytest.raises(L.LawInvalidInput):
            L._norm_date(raw)

    def test_연혁_정렬은_공포일자까지_본다(self):
        """같은 시행일자에 여러 공포본이 있을 때(근기법 20070701) 옛 공포본이 선택되면
        서면 명시 의무 도입 시점을 정반대로 답한다."""
        rows = [
            {"시행일자": "20070701", "공포일자": "20070126", "공포번호": "8293", "MST": "77179"},
            {"시행일자": "20070701", "공포일자": "20061221", "공포번호": "8080", "MST": "76363"},
        ]
        rows.sort(key=lambda r: (r.get("시행일자", ""), r.get("공포일자", ""),
                                 str(r.get("공포번호", "")).zfill(10)))
        assert rows[-1]["MST"] == "77179"

    def test_인식_불가한_court_값은_거부한다(self):
        """'하위법원'은 law.go.kr이 받지 않아 항상 0건 → '판례 없음'으로 둔갑했다."""
        with pytest.raises(L.LawInvalidInput):
            L.LawGoKrClient().search_cases("통상임금", court="하위법원")


# ---------------------------------------------------------------------------
# 5. 임금대장 — 자료가 없는데 '적법'으로 새던 경로
# ---------------------------------------------------------------------------

def _data(월별, n=10, **emp):
    return {"사업장": {"상시근로자수": n},
            "직원": [dict({"사원ID": "A", "주소정근로시간": 40, "월별": 월별}, **emp)]}


def _verdict(res, 검사):
    return [t["판정"] for t in res["직원별"][0]["월별판정"] if t["검사항목"] == 검사]


class Test임금대장:
    기본 = [{"명칭": "기본급", "금액": 2_500_000, "성격": "기본급"}]

    def test_시간필드_일부만_기재되면_가산수당은_불확실(self):
        """예전: 셋 중 하나라도 값이 있으면 나머지를 0시간으로 간주하고 '적법'.
        야간 30h가 실제로 있었다면 188,285원 체불이 덮였다."""
        res = pr.analyze_payroll(_data([{
            "연월": "2026-03", "임금항목": self.기본 + [
                {"명칭": "연장수당", "금액": 358_852, "성격": "연장수당"}],
            "연장시간": 20.0, "야간시간": None, "휴일시간": None,
            "근로일수": 21, "총근로시간": 229}]))
        assert _verdict(res, "가산수당") == ["불확실"]
        assert _verdict(res, "기재사항") == ["불확실"]

    def test_입사월은_최저임금_판정을_확정하지_않는다(self):
        """예전: 3/20 입사자 한 명으로 '위반' + 총부족액 1,256,880원(허위)."""
        res = pr.analyze_payroll(_data(
            [{"연월": "2026-03", "임금항목": [{"명칭": "기본급", "금액": 900_000, "성격": "기본급"}],
              "근로일수": 8}], 입사일="2026-03-20"))
        assert _verdict(res, "최저임금") == ["불확실"]
        assert res["요약"]["부족액추정"].get("최저임금", 0) == 0

    def test_임금항목이_없으면_통상임금도_최저임금도_불확실(self):
        res = pr.analyze_payroll(_data([{
            "연월": "2026-03", "임금항목": [], "근로일수": 21, "총근로시간": 209,
            "연장시간": 0, "야간시간": 0, "휴일시간": 0}]))
        assert _verdict(res, "통상임금") == ["불확실"]
        assert _verdict(res, "최저임금") == ["불확실"]

    @pytest.mark.parametrize("금액", [None, "2,500,000", "2500000"])
    def test_엑셀에서_온_금액_표기를_받아준다(self, 금액):
        """예전: None은 TypeError, '2,500,000'은 ValueError로 도구 자체가 죽었다."""
        pr.analyze_payroll(_data([{"연월": "2026-03",
                                   "임금항목": [{"명칭": "기본급", "금액": 금액}]}]))

    def test_음수_금액은_명시적으로_거부한다(self):
        with pytest.raises(ValueError):
            pr.analyze_payroll(_data([{"연월": "2026-03",
                                       "임금항목": [{"명칭": "공제", "금액": -100_000}]}]))

    def test_연월_중복은_이중계상하지_않는다(self):
        res = pr.analyze_payroll(_data([
            {"연월": "2026-03", "임금항목": self.기본},
            {"연월": "2026-03", "임금항목": self.기본}]))
        assert len(_verdict(res, "최저임금")) == 1
        assert any("중복" in w for w in res["경고"])

    def test_상시근로자수_미지정은_가정을_밝힌다(self):
        res = pr.analyze_payroll({"사업장": {}, "직원": [
            {"사원ID": "A", "주소정근로시간": 40,
             "월별": [{"연월": "2026-03", "임금항목": self.기본}]}]})
        assert any("5인" in w and "가정" in w for w in res["경고"])

    def test_대규모_대장도_응답_크기를_제어한다(self):
        """예전: 20명×12개월 = 824,854자로 컨텍스트를 태웠다."""
        직원 = [{"사원ID": f"E{i}", "주소정근로시간": 40,
                "월별": [{"연월": f"2026-{m:02d}", "임금항목": self.기본,
                        "연장시간": 10.0, "야간시간": 0, "휴일시간": 0,
                        "근로일수": 21, "총근로시간": 219} for m in range(1, 13)]}
               for i in range(20)]
        res = pr.analyze_payroll({"사업장": {"상시근로자수": 10}, "직원": 직원})
        assert "잘림" in res
        assert len(repr(res)) < 200_000


class Test급여설계:
    def test_고정OT가_주12시간_한도를_넘으면_경고한다(self):
        """예전: 월 100시간(주 평균 23시간)짜리 고정OT 계약서 문안을 경고 없이 생성했다."""
        r = pr.design_pay_table({"연도": 2026, "주소정근로시간": 40, "상시근로자수": 10,
                                 "목표": {"방식": "월총액", "금액": 5_000_000},
                                 "고정연장시간_월": 100})
        assert r["검증"]["법정한도판정"] == "위반의심"
        assert any("§53①" in c for c in r["주의사항"])

    def test_연봉방식에서_비매월_고정수당도_차감한다(self):
        """예전: 정기상여만 빼고 고정수당은 누락해 연봉환산이 목표를 초과했다(+120만)."""
        r = pr.design_pay_table({
            "연도": 2026, "주소정근로시간": 40, "상시근로자수": 10,
            "목표": {"방식": "연봉", "금액": 36_000_000},
            "고정수당": [{"명칭": "명절수당", "금액": 600_000, "지급주기": "반기", "성격": "기타"}],
            "고정연장시간_월": 20})
        assert r["급여테이블"]["연봉환산"] == pytest.approx(36_000_000, abs=1)

    def test_주소정근로시간_0은_ValueError(self):
        with pytest.raises(ValueError):
            pr.design_pay_table({"연도": 2026, "주소정근로시간": 0, "상시근로자수": 10,
                                 "목표": {"방식": "월총액", "금액": 3_000_000}})


# ---------------------------------------------------------------------------
# 6. verify_citations — 인용 환각 차단 (v1.1 신규)
# ---------------------------------------------------------------------------

import server as S  # noqa: E402  (FastMCP 구성만 하고 서버는 띄우지 않는다)


class Test인용검증:
    @pytest.mark.parametrize("cite,kind", [
        ("2020다247190", "판례"),
        ("2021다227100", "판례"),
        ("근로기준정책과-3084", "행정해석"),
        ("근기 68207-2140", "행정해석"),
        ("07-0039", "법령해석례"),
        ("중앙2024부해1234", "노동위원회"),
        ("2026부해123", "노동위원회"),
    ])
    def test_문서번호_표기로_자료원을_라우팅한다(self, cite, kind):
        assert S._cite_kind(cite) == kind

    def test_접미일치는_기관명_생략만_인정한다(self):
        """실무 축약은 앞쪽 기관명을 생략한다("기획재정부 근로기준정책과-73" →
        "근로기준정책과-73"). 접미로 한정해야 숫자 경계 오탐(-73이 -732에 걸림)과
        연도만 있는 입력의 광범위 오탐이 동시에 막힌다."""
        assert S._cite_match("근로기준정책과-73", "기획재정부 근로기준정책과-73")
        assert not S._cite_match("근로기준정책과-73", "근로기준정책과-732")
        assert not S._cite_match("2024", "2024다12345")
        assert S._cite_match("2020다247190", "2020다247190")

    def test_한번에_10건까지만_받는다(self):
        r = S.verify_citations(["2020다247190"] * 11)
        assert r["status"] == "INVALID_INPUT"

    def test_형식을_모르면_미확인이지_확인이_아니다(self):
        r = S.verify_citations(["헛소리"])
        assert r["검증결과"][0]["판정"] == "미확인"

    def test_한계_고지가_항상_붙는다(self):
        r = S.verify_citations(["헛소리"])
        assert any("뒷받침한다는 뜻은" in h for h in r["한계"])


@pytest.mark.live
class Test인용검증_실호출:
    def test_실존_판례와_가짜_판례를_가른다(self):
        r = S.verify_citations(["2020다247190", "2020다000000"])
        판정 = {x["인용"]: x["판정"] for x in r["검증결과"]}
        assert 판정["2020다247190"] == "확인"
        assert 판정["2020다000000"] == "미확인"

    def test_확인된_판례에는_OC_없는_출처가_붙는다(self):
        r = S.verify_citations(["2021다227100"])
        x = r["검증결과"][0]
        assert x["판정"] == "확인"
        assert x["출처"].startswith("https://www.law.go.kr/precInfoP.do?precSeq=")
        assert "OC=" not in x["출처"]

    def test_자료원_상태_점검(self):
        r = S.check_sources_health()
        assert r["전체"] == 4
        assert all("status" in x for x in r["자료원상태"])
