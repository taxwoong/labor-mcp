# -*- coding: utf-8 -*-
"""
test_law_eflaw.py — 시행본 문안을 시행일 기준 통합본(target=eflaw)으로 받는지 (오프라인)

2026-10-09 실측: 시행본 문안을 lawService target=law&MST=…(그 MST 공포 당시 원문)로 받아,
같은 MST가 더 늦은 시행일자 행으로 현행이 되면 그 MST보다 늦게 공포됐지만 먼저 시행된 개정이
빠지고(근로기준법 2026.8.4 타법개정, 국민연금법 2026.5.26 개정), 아직 시행 전인 문안이 섞였다
(근로기준법 제101조 '노동감독관'). law.go.kr 응답(_get)을 가짜 XML로 바꿔 끼워 확인한다.
법령·MST·공포번호는 모두 시험용 가짜 값. '오늘'(law_go_kr._today_kst)도 가짜 값으로 고정한다.

실행:
    python tests/test_law_eflaw.py
    python -m pytest tests/test_law_eflaw.py -q
"""
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import law_go_kr  # noqa: E402
from law_go_kr import LawInvalidInput, LawNotFound, LawUpstreamError  # noqa: E402

NO_MATCH = ('<?xml version="1.0" encoding="utf-8"?><Law>일치하는 법령이 없습니다.  '
            '법령명을 확인하여 주십시오.</Law>')  # law.go.kr 실제 응답 형태


def _list_xml(rows):
    return "<LawSearch>" + "".join(
        f"<law id='{i}'><법령명한글><![CDATA[{r['법령명']}]]></법령명한글><법령ID>{r['법령ID']}</법령ID>"
        f"<법령일련번호>{r['MST']}</법령일련번호><시행일자>{r['시행일자']}</시행일자>"
        f"<공포일자>{r['공포일자']}</공포일자><공포번호>{r['공포번호']}</공포번호>"
        f"<제개정구분명>{r['제개정구분']}</제개정구분명></law>"
        for i, r in enumerate(rows, 1)) + "</LawSearch>"


def _law_xml(name, law_id, pub, no, eff, articles, addenda, staged=""):
    """articles: [(조번호, 제목, 본문, 조문참고자료)], addenda: [(공포일자, 공포번호, 본문)],
    staged: 헤더 <조문시행일자문자열> (예: '20261208:제101조')"""
    arts = "".join(
        f"<조문단위 조문키='{n}'><조문번호>{n.split('의')[0]}</조문번호>"
        + (f"<조문가지번호>{n.split('의')[1]}</조문가지번호>" if "의" in n else "")
        + f"<조문여부>조문</조문여부><조문제목><![CDATA[{t}]]></조문제목><조문시행일자>{eff}</조문시행일자>"
        f"<조문내용><![CDATA[제{n.replace('의', '조의', 1) if '의' in n else n + '조'}({t}) {body}]]></조문내용>"
        + (f"<조문참고자료><![CDATA[{ref}]]></조문참고자료>" if ref else "") + "</조문단위>"
        for n, t, body, ref in articles)
    adds = "".join(
        f"<부칙단위 부칙키='{d}{k}'><부칙공포일자>{d}</부칙공포일자><부칙공포번호>{k}</부칙공포번호>"
        f"<부칙내용><![CDATA[{b}]]></부칙내용></부칙단위>" for d, k, b in addenda)
    return (f"<?xml version='1.0' encoding='UTF-8'?><법령><기본정보><법령ID>{law_id}</법령ID>"
            f"<공포일자>{pub}</공포일자><공포번호>{no}</공포번호>"
            f"<법령명_한글><![CDATA[{name}]]></법령명_한글><시행일자>{eff}</시행일자>"
            f"<제개정구분>일부개정</제개정구분>"
            + (f"<조문시행일자문자열>{staged}</조문시행일자문자열>" if staged else "")
            + f"</기본정보><조문>{arts}</조문><부칙>{adds}</부칙></법령>")


def _row(mst, eff, pub, no, kind="일부개정", name="라법", law_id="900003"):
    return {"법령명": name, "법령ID": law_id, "MST": mst, "시행일자": eff, "공포일자": pub,
            "공포번호": no, "제개정구분": kind}


def _upstream(cause):
    """실제 _get처럼 requests 예외를 LawUpstreamError로 감싼다."""
    err = LawUpstreamError(f"law.go.kr 접속 실패: {type(cause).__name__}: {cause}")
    err.__cause__ = cause
    return err


def _raises(exc_type, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc_type as e:
        return e
    raise AssertionError(f"{exc_type.__name__}가 나야 함")


class FakeLawGoKr:
    """law_go_kr._get 대역. docs 키: ("law", MST) 또는 ("eflaw", MST, efYd). today: 가짜 '오늘'."""

    def __init__(self, history, current, docs, errors=None, today="20261009"):
        self.history, self.current, self.docs = history, current, docs
        self.errors = errors or {}
        self.today = today
        self.calls = []

    def __call__(self, endpoint, **params):
        t = params.get("target")
        key = ("eflaw", params.get("MST"), params.get("efYd")) if t == "eflaw" else ("law", params.get("MST"))
        self.calls.append((endpoint,) + key)
        if endpoint == "lawSearch.do":
            if t == "eflaw":
                return _list_xml(self.history) if params.get("page", 1) == 1 else "<LawSearch></LawSearch>"
            return _list_xml(self.current)
        if key in self.errors:
            raise self.errors[key]
        return self.docs.get(key, NO_MATCH)

    def __enter__(self):
        self.orig = law_go_kr._get
        self.orig_today = law_go_kr._today_kst
        law_go_kr._get = self
        law_go_kr._today_kst = lambda: self.today
        return law_go_kr.LawGoKrClient()

    def __exit__(self, *exc):
        law_go_kr._get = self.orig
        law_go_kr._today_kst = self.orig_today
        return False


# ---------------------------------------------------------------------------
# 근로기준법 실측 사례를 본뜬 라법 (법령ID 900003), 오늘 = 2026.10.9
#   제533호(2026.4.7 공포): 2026.10.8 시행, 단 제101조·제116조제2항제1호는 2026.12.8, 제44조의4는 2027.1.1
#   제857호(2026.8.4 공포, 타법개정): 2026.10.2 시행, 제13조 개정
#   제472호(2026.3.17 공포, 타법개정): 2027.1.1 시행 — 오늘 현재 공포됐지만 아직 시행 전, 부칙에서 제43조의6 개정
# 연혁(시행일자 순): 290@20261002 · 285@20261008 · 285@20261208 · 284@20270101 · 285@20270101
# 2026.10.9 현행 = 285@20261008 — MST 285 공포 당시 원문에는 제857호 개정이 없고,
# 2026.12.8 시행 예정 제101조 문안과 2027.1.1 신설 제44조의4가 미리 들어 있다.
# 실제 MST 290781(2026.8.4 공포) 원문처럼 290 공포 당시 원문에도 시행 전 제101조 신문안이 들어 있다.
# ---------------------------------------------------------------------------
STAGED_HDR = "20261208:제101조,제116조제2항제1호 20270101:제44조의4"
A_533 = ("20260407", "533", "부칙 <제533호,2026.4.7>\n제1조(시행일) 이 법은 공포 후 6개월이 경과한 날부터 "
                            "시행한다. 다만, 제101조의 개정규정은 2026년 12월 8일부터 시행한다.")
A_857 = ("20260804", "857", "부칙 <제857호,2026.8.4>\n제1조(시행일) 이 법은 2026년 10월 2일부터 시행한다.")
A_472 = ("20260317", "472", "부칙 <제472호,2026.3.17>\n제1조(시행일) 이 법은 2027년 1월 1일부터 시행한다.\n"
                            "제9조(다른 법률의 개정) 라법 일부를 다음과 같이 개정한다. 제43조의6제1항제4호 중 "
                            "\"월평균보수\"를 \"월 보수\"로 한다.")
OLD_101 = ("101", "감독 기관", "근로감독관을 둔다.", "")
NEW_101 = ("101", "감독 기관", "노동감독관의 권한은 「노동감독관 직무집행법」에서 정한다.", "")
OLD_116 = ("116", "과태료", "1천만원 이하의 과태료를 부과한다. <신설 2021.4.13>", "")
NEW_116 = ("116", "과태료", "다음 각 호의 경우 1천만원 이하의 과태료를 부과한다. <신설 2021.4.13, 2026.4.7>", "")
OLD_13 = ("13", "보고", "근로감독관에게 보고하여야 한다.", "")
NEW_13 = ("13", "보고", "근로감독관 또는 노동위원회에 보고하여야 한다. <개정 2026.8.4>", "")
ART_444 = ("44의4", "임금비용의 구분지급", "도급인은 임금비용을 구분하여 지급하여야 한다.", "")
ART_436 = ("43의6", "체불사업주 명단공개", "월평균보수를 기준으로 한다.", "")


def _labor_fake(**over):
    history = [_row("290", "20261002", "20260804", "857", kind="타법개정"),
               _row("285", "20261008", "20260407", "533"),
               _row("285", "20261208", "20260407", "533"),
               _row("284", "20270101", "20260317", "472", kind="타법개정"),
               _row("285", "20270101", "20260407", "533")]
    current = [_row("285", "20261008", "20260407", "533")]
    x = lambda pub, no, eff, arts, adds, staged="": _law_xml(  # noqa: E731
        "라법", "900003", pub, no, eff, arts, adds, staged)
    docs = {
        # 공포 당시 원문 — 시행 전 문안(제101조 신문안·제44조의4)이 미리, 늦게 공포된 제857호는 없음
        ("law", "285"): x("20260407", "533", "20261008", [OLD_13, ART_444, NEW_101, NEW_116, ART_436],
                          [A_472, A_533], STAGED_HDR),
        ("law", "290"): x("20260804", "857", "20261002", [NEW_13, NEW_101, NEW_116, ART_436],
                          [A_472, A_533, A_857]),
        ("law", "284"): x("20260317", "472", "20270101", [OLD_13, OLD_101, OLD_116, ART_436], [A_472]),
        # 시행일 기준 통합본 — 헤더 일정 문자열은 공포 당시 그대로 실린다
        ("eflaw", "290", "20261002"): x("20260804", "857", "20261002", [NEW_13, OLD_101, OLD_116, ART_436],
                                        [A_857]),
        ("eflaw", "285", "20261008"): x("20260407", "533", "20261008", [NEW_13, OLD_101, NEW_116, ART_436],
                                        [A_533, A_857], STAGED_HDR),
        ("eflaw", "285", "20261208"): x("20260407", "533", "20261208", [NEW_13, NEW_101, NEW_116, ART_436],
                                        [A_533, A_857], STAGED_HDR),
    }
    for k in over.pop("drop", []):
        docs.pop(k)
    docs.update(over.pop("docs", {}))
    return FakeLawGoKr(history, current, docs, errors=over.pop("errors", None), today=over.pop("today", "20261009"))


def test_current_article_includes_later_promulgated_amendment():
    fake = _labor_fake()
    with fake as client:
        art = client.law_article_as_of("라법", "20261009", "13")
    assert "노동위원회" in art["원문"], art["원문"]       # 2026.8.4 공포·2026.10.2 시행 타법개정 반영
    assert (art["적용시행본"]["MST"], art["적용시행본"]["시행일자"]) == ("285", "20261008"), art["적용시행본"]
    assert "주의" not in art, art.get("주의")
    assert "조문시행일자" not in art, "통합본 조문시행일자는 늘 시행본 날짜 — 조문별 시행일로 오독됨"
    assert "〈개정 2026.8.4〉" in art["원문"], "개정 주석이 태그 제거에 지워짐"
    assert ("lawService.do", "eflaw", "285", "20261008") in fake.calls
    assert ("lawService.do", "law", "285") not in fake.calls, "통합본 경로가 공포 당시 원문을 요청함"


def test_not_yet_effective_text_is_not_returned():
    with _labor_fake() as client:
        art = client.law_article_as_of("라법", "20261009", "101")
        later = client.law_article_as_of("라법", "20261209", "101")
    assert "근로감독관을 둔다" in art["원문"] and "노동감독관" not in art["원문"], art["원문"]
    assert "노동감독관" in later["원문"] and later["적용시행본"]["시행일자"] == "20261208", later


def test_header_schedule_does_not_trigger_backtracking_on_consolidated_text():
    """통합본 헤더에도 '20261208:…제116조제2항제1호'가 그대로 실린다 — 이것으로 소급하면
    20261008 시행분 개정(2026.4.7)을 잃는다 (근로기준법 제116조 실측)."""
    with _labor_fake() as client:
        art = client.law_article_as_of("라법", "20261009", "116")
    assert "다음 각 호의 경우" in art["원문"], art["원문"]
    assert art["적용시행본"]["MST"] == "285" and "주의" not in art, art


def test_article_not_yet_created_is_not_found():
    """2027.1.1 신설 조문은 2026.10.9 통합본에 없다 — 예전처럼 미래 문안을 돌려주면 안 된다."""
    with _labor_fake() as client:
        e = _raises(LawNotFound, client.law_article_as_of, "라법", "20261009", "44의4")
    assert "신설 전" in str(e) and "제44조의4" in str(e), e


def test_article_no_forms_are_normalized_and_bad_forms_are_invalid_input():
    """'제13조'·'13조'·'013'은 같은 조문, '13-2'·'제13조제1항'은 INVALID_INPUT — NOT_FOUND(부존재)로 나가면
    '근로기준법 제17조는 없다'로 읽힌다 (2026-10-09 실측: '제17조'·'17조'·'76의 2' → NOT_FOUND)."""
    with _labor_fake() as client:
        for a in ("제13조", "13조", "013", " 13 "):
            assert client.law_article_as_of("라법", "20261009", a)["조문"] == "제13조", a
        assert client.law_article_as_of("라법", "20270101", "44의 4")["조문"] == "제44조의4"
        for bad in ("13-2", "제13조제1항", "십삼"):
            _raises(LawInvalidInput, client.law_article_as_of, "라법", "20261009", bad)


def test_name_must_match_exactly_without_law_id():
    """law_id 없이 이름이 정확히 같은 법령이 연혁에 없으면 다른 법령 조문을 OK로 내지 않는다 (INVALID_INPUT)
    (2026-10-09 실측: law='고용보험' 제2조가 고용산재보험료징수법 제2조로 status OK)."""
    with _labor_fake() as client:
        e = _raises(LawInvalidInput, client.law_article_as_of, "라", "20261009", "13")
        assert "라법" in str(e), e
        assert client.law_article_as_of("라 법", "20261009", "13")["조문"] == "제13조"   # 공백 차이 무시


def test_same_name_multiple_ids_keep_only_that_law():
    """이름이 같은 법령ID가 둘이면 그 둘로 거르고 시행령·시행규칙은 뺀다 (2026-10-09 실측:
    '노동조합및노동관계조정법'(000142·000143) 제2조가 시행규칙 제2조로 status OK)."""
    rows = [_row("1", "19970301", "19961231", "5244", kind="제정", name="노동조합및노동관계조정법", law_id="000142"),
            _row("2", "19970313", "19970313", "5310", kind="폐지", name="노동조합및노동관계조정법", law_id="000142"),
            _row("3", "19970313", "19970313", "5310", kind="제정", name="노동조합및노동관계조정법", law_id="000143"),
            _row("4", "20260310", "20260310", "1", name="노동조합 및 노동관계조정법 시행규칙", law_id="006935"),
            _row("5", "20251201", "20251201", "21000", name="노동조합 및 노동관계조정법", law_id="000143")]
    with FakeLawGoKr(rows, rows[-1:], {}) as client:
        hist = client.law_history("노동조합및노동관계조정법")
    assert [r["MST"] for r in hist] == ["1", "2", "3", "5"], [r["MST"] for r in hist]


def test_fallback_returns_that_rows_promulgated_text_without_backtracking():
    """통합본 조회 실패 → 그 행 MST의 공포 당시 원문 + 맨 앞 주의 ('시행일 기준 문안 확인 실패'). 예전처럼
    헤더 일정으로 소급하지 않고 '소급 선택했습니다'로 정당화하지도 않는다 — 소급한 공포본 원문에도 시행 전
    문안이 이미 들어 있어 실측 5건이 모두 틀렸다 (2026-10-09: 근로기준법 제101조 MST 290781 '노동감독관')."""
    with _labor_fake(drop=[("eflaw", "285", "20261008")]) as client:
        art = client.law_article_as_of("라법", "20261009", "101")
    assert art["적용시행본"]["MST"] == "285", art["적용시행본"]
    assert art["주의"].startswith("시행일 기준 문안 확인 실패"), art["주의"]
    assert "소급" not in art["주의"] and "일치하는 법령 없음" in art["주의"], art["주의"]
    assert "기준일 현재 문안이 아닐 수 있" in art["주의"], art["주의"]


def test_fallback_without_the_article_is_upstream_not_not_found():
    """폴백한 공포 당시 원문에 조문이 없다고 시행본에 없는 것은 아니다 — NOT_FOUND가 아니라 UPSTREAM."""
    with _labor_fake(drop=[("eflaw", "285", "20261008")]) as client:
        e = _raises(LawUpstreamError, client.law_article_as_of, "라법", "20261009", "99")
    assert "단정할 수 없음" in str(e), e


def test_fallback_on_http_500_but_not_on_connection_error():
    resp = requests.Response()
    resp.status_code = 500
    http500 = _upstream(requests.exceptions.HTTPError("500 Server Error", response=resp))
    with _labor_fake(errors={("eflaw", "285", "20261008"): http500}) as client:
        art = client.law_article_as_of("라법", "20261009", "13")
    assert "HTTP 500" in art["주의"], art.get("주의")
    down = _upstream(requests.exceptions.ConnectionError("연결 실패"))
    with _labor_fake(errors={("eflaw", "285", "20261008"): down}) as client:
        _raises(LawUpstreamError, client.law_article_as_of, "라법", "20261009", "13")


def test_addenda_current_includes_later_promulgated_and_promul_no_fallback():
    fake = _labor_fake()
    with fake as client:
        cur = client.law_addenda(law_name="라법")
        by_mst = client.law_addenda(mst="285")                              # 오늘 → 285@20261008 통합본
        by_mst_later = client.law_addenda(mst="285", as_of_date="20261209")  # → 285@20261208
        future = client.law_addenda(law_name="라법", promul_no="472")
    assert [r["공포번호"] for r in cur["부칙목록_최신순"]] == ["857", "533"], cur
    assert "통합본" in cur["부칙범위"] and "주의" not in cur, cur
    assert [r["공포번호"] for r in by_mst["부칙목록_최신순"]] == ["857", "533"], by_mst
    assert by_mst["시행본_시행일자"] == "20261008" and "통합본" in by_mst["부칙범위"], by_mst
    assert by_mst_later["시행본_시행일자"] == "20261208", by_mst_later
    assert future["부칙"]["공포번호"] == "472" and "20261009 현재 아직 시행 전" in future["주의"], future
    assert ("lawService.do", "law", "284") in fake.calls


def test_addenda_lists_and_searches_pending_promulgations():
    """기준일 현재 공포됐지만 아직 시행 전인 개정(제472호)은 통합본 부칙에 없다 — 목록 모드는 '시행전_개정',
    article_no 모드는 그 공포본 원문 부칙에서 '시행전_개정_부칙_언급' (2026-10-09 실측: 근로기준법 '43의6' →
    제21472호 부칙 제9조가 빠지고 '별도 적용례가 없었다는 뜻일 수 있음'으로 안내됨)."""
    fake = _labor_fake()
    with fake as client:
        lst = client.law_addenda(law_name="라법")
        art = client.law_addenda(law_name="라법", article_no="43의6")
    assert [p["공포번호"] for p in lst.get("시행전_개정", [])] == ["472"], lst
    assert art["언급된_부칙_최신순"] == [], art
    assert [p["공포번호"] for p in art.get("시행전_개정_부칙_언급", [])] == ["472"], art
    assert "제43조의6제1항제4호" in art["시행전_개정_부칙_언급"][0]["해당항"][-1], art
    assert "안내" not in art and "아직 시행 전" in art["주의"], art
    assert ("lawService.do", "law", "290") in fake.calls     # 기준일 이전 마지막 공포본 원문


def test_addenda_article_pattern_allows_possessive():
    """'제30조의 개정규정'은 제30조 언급이다 — 예전 (?!의) 가드가 걸렀다 (노인장기요양보험법 제30조 실측)."""
    add = ("20260526", "690", "부칙 <제690호,2026.5.26>\n제1조(시행일) 이 법은 2026년 11월 27일부터 시행한다. "
                              "다만, 제30조의 개정규정은 공포한 날부터 시행한다.")
    hist = [_row("700", "20260526", "20260526", "690")]
    doc = _law_xml("라법", "900003", "20260526", "690", "20260526", [OLD_13], [add])
    with FakeLawGoKr(hist, hist, {("eflaw", "700", "20260526"): doc}, today="20260601") as client:
        out = client.law_addenda(law_name="라법", article_no="30")
        out2 = client.law_addenda(law_name="라법", article_no="3")
    assert [x["공포번호"] for x in out["언급된_부칙_최신순"]] == ["690"], out
    assert out2["언급된_부칙_최신순"] == [], "제3조가 제30조에 오매치"
    p = law_go_kr._article_pat("제59조의2")
    assert p.search("제59조의2제1항") and not p.search("제59조의21")


def test_partially_staged_article_is_flagged():
    ref_116 = ("116", "과태료", "다음 각 호의 경우 1천만원 이하의 과태료를 부과한다.",
               "[시행일: 2027.1.1] 제116조제1항제2호")
    doc = _law_xml("라법", "900003", "20260407", "533", "20261008", [NEW_13, OLD_101, ref_116],
                   [A_533, A_857], STAGED_HDR)
    with _labor_fake(docs={("eflaw", "285", "20261008"): doc}) as client:
        art = client.law_article_as_of("라법", "20261009", "116")
    assert art["시행예정_부분"] == [{"시행일자": "20270101", "대상": "제116조제1항제2호"}], art
    assert "제116조제1항제2호: 20270101 시행" in art["주의"], art["주의"]


def test_conditional_staged_marker_by_workplace_size():
    """사업장 규모별 '[시행일] … 다음 각 호의 구분에 따른 날' 형식 (2026-10-09 실측: 산업안전보건법 제175조
    20270101 통합본 — 50명 미만 사업장은 2028.1.1, 예전에는 시행예정_부분·주의가 모두 None)."""
    ref = ("[시행일] 제116조제4항제2호의2, 제116조제5항제1호의 개정규정은 다음 각 호의 구분에 따른 날\n"
           "  1. 상시 50명 이상의 근로자를 사용하는 사업장(건설업의 경우에는 공사 금액이 50억원 이상인 사업장): 2027년 1월 1일\n"
           "  2. 상시 50명 미만의 근로자를 사용하는 사업장(건설업의 경우에는 공사 금액이 50억원 미만인 사업장): 2028년 1월 1일")
    art116 = ("116", "과태료", "다음 각 호의 경우 과태료를 부과한다.", ref)
    doc = _law_xml("라법", "900003", "20260407", "533", "20270101", [NEW_13, NEW_101, art116], [A_533])
    with _labor_fake(docs={("eflaw", "285", "20270101"): doc}, today="20270201") as client:
        art = client.law_article_as_of("라법", "20270201", "116")     # 행: 285@20270101 (기준일 = 오늘)
    got = art.get("시행예정_부분", [])
    assert [s["시행일자"] for s in got] == ["20280101"], got       # 기준일(2027.2.1) 뒤의 것만
    assert got[0]["대상"].startswith("제116조제4항제2호의2") and got[0]["조건"].startswith("상시 50명 미만"), got
    assert "사업장 규모" in art["주의"], art.get("주의")


def test_past_anachronism_gets_promulgated_reference():
    """과거 기준일에 통합본 문안의 개정 주석이 기준일보다 늦으면 '주의' + 기준일 이전 마지막 공포본의 같은 조문을
    '공포본_참고'로 (함정 6 — labor 표본에서는 사례 0건이지만 세법과 같은 장치를 둔다)."""
    a_new = ("15", "특례", "특례는 2027년까지 적용한다. <신설 2024.1.9, 2026.3.17>", "")
    a_old = ("15", "특례", "특례는 2026년까지 적용한다. <신설 2024.1.9>", "")
    hist = [_row("600", "20250101", "20240109", "100"), _row("610", "20260401", "20260317", "200")]
    docs = {("eflaw", "600", "20250101"): _law_xml("라법", "900003", "20240109", "100", "20250101", [a_new], []),
            ("law", "600"): _law_xml("라법", "900003", "20240109", "100", "20250101", [a_old], [])}
    fake = FakeLawGoKr(hist, hist[-1:], docs, today="20261009")
    with fake as client:
        art = client.law_article_as_of("라법", "20250601", "15")
    assert "2027년" in art["원문"] and "2026년" in art["공포본_참고"]["원문"], art
    assert "20260317" in art["주의"] and "공포본_참고" in art["주의"], art.get("주의")


def test_resolve_law_aliases_and_spacing():
    """통칭·붙여쓰기·가운뎃점 차이를 정식 법령명으로 (2026-10-09 실측: '노동조합및노동관계조정법'은 법령ID 필터를
    못 받아 시행규칙 조문, '고용산재보험료징수법'·'산재보험법'·'중대재해처벌법'은 조문 모드 NOT_FOUND)."""
    import server as S
    import labor_constants as LC
    assert S._resolve_law("노동조합및노동관계조정법") == ("노동조합 및 노동관계조정법", LC.LAW_IDS["노동조합 및 노동관계조정법"])
    assert S._resolve_law("남녀고용평등과 일·가정 양립 지원에 관한 법률")[1] == LC.LAW_IDS[
        "남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률"]
    assert S._resolve_law("고용산재보험료징수법")[0] == "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률"
    assert S._resolve_law("산재보험법 시행령") == ("산업재해보상보험법 시행령", "")
    assert S._resolve_law("근기법시행규칙") == ("근로기준법 시행규칙", "")
    assert S._resolve_law("근로기준법") == ("근로기준법", LC.LAW_IDS["근로기준법"])
    assert S._resolve_law("모르는법") == ("모르는법", "")


def test_verify_rates_reads_current_consolidated_text():
    """요율 대조(check_sources_health)도 현행 검색 결과의 시행일자로 통합본을 읽어야 한다."""
    import social_insurance as si
    calls = []
    orig_search, orig_article = law_go_kr.LawGoKrClient.search_laws, law_go_kr.LawGoKrClient.law_article
    law_go_kr.LawGoKrClient.search_laws = lambda self, name, display=20: [
        {"법령명": name, "MST": "1", "시행일자": "20260101"}]

    def fake_article(self, mst, art_no, max_chars=6000, ef_yd="", **kw):
        calls.append((mst, art_no, ef_yd))
        return {"원문": ""}

    law_go_kr.LawGoKrClient.law_article = fake_article
    try:
        si.verify_rates(2026)
    finally:
        law_go_kr.LawGoKrClient.search_laws, law_go_kr.LawGoKrClient.law_article = orig_search, orig_article
    assert calls and all(ef == "20260101" for _m, _a, ef in calls), calls


def test_verify_rates_reports_missing_table_year_not_lookup_failure():
    """요율표에 그 해 값이 없으면 '조회 실패: TypeError'·상태 OK가 아니라 '표 미갱신'으로 (2027년분 표 누락 대비)."""
    import social_insurance as si
    orig_search, orig_article = law_go_kr.LawGoKrClient.search_laws, law_go_kr.LawGoKrClient.law_article
    texts = {"44": "1만분의 719", "4": "100만분의 9448", "12": "실업급여의 보험료율: 1천분의 18"}
    law_go_kr.LawGoKrClient.search_laws = lambda self, name, display=20: [
        {"법령명": name, "MST": "1", "시행일자": "20270101"}]
    law_go_kr.LawGoKrClient.law_article = lambda self, mst, art_no, max_chars=6000, ef_yd="", **kw: {
        "원문": texts.get(art_no, "")}
    try:
        out = si.verify_rates(2999)
    finally:
        law_go_kr.LawGoKrClient.search_laws, law_go_kr.LawGoKrClient.law_article = orig_search, orig_article
    assert out["상태"].startswith("불일치") and "요율표 미갱신" in out["상태"], out   # 요율경고가 뜨도록
    assert not any("TypeError" in str(i.get("근거")) for i in out["항목"]), out["항목"]


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, f"{type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
