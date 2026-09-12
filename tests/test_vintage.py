# -*- coding: utf-8 -*-
"""test_vintage.py — 검색 결과 시점 대조 (판례 변경 전 자료 경고)"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import labor_constants as LC  # noqa: E402
import vintage  # noqa: E402

TODAY = "2026-09-04"


@pytest.mark.parametrize("raw,expected", [
    ("2021-10-14", "2021-10-14"), ("2016.5.9.", "2016-05-09"), ("2016.05.09", "2016-05-09"),
    ("20211014", "2021-10-14"), ("2019", "2019-01-01"), ("", ""), ("미상", ""),
])
def test_iso_date(raw, expected):
    assert vintage.iso_date(raw) == expected


def test_item_date_prefers_iso_then_source_specific_keys():
    assert vintage.item_date({"일자_ISO": "2020-01-02", "일자": "미상"}) == "2020-01-02"
    assert vintage.item_date({"해석일자": "2018.04.10"}) == "2018-04-10"
    assert vintage.item_date({"판정일": "2016.5.9."}) == "2016-05-09"
    assert vintage.item_date({"등록일": "2026.09.03"}) == "2026-09-03"
    assert vintage.item_date({"사건명": "x"}) == ""
    # v1.3 원천 — 여기 없으면 일자가 멀쩡한 자료를 '일자미상'으로 세어 엉뚱한 주의가 붙는다
    assert vintage.item_date({"재결일자": "2025-06-26"}) == "2025-06-26"   # 건강보험분쟁조정위
    assert vintage.item_date({"결정년도": "2020년"}) == "2020-01-01"       # 국민연금 (연도만 공개)


def test_warns_on_pre_turning_point_document():
    items = [{"제목": "통상임금 산정 — 재직조건부 상여금", "일자": "2015-03-02"}]
    info = vintage.annotate(items, keyword="통상임금", today=TODAY)
    w = info["시점주의"]
    assert len(w) == 1 and w[0]["쟁점"] == "통상임금 고정성 폐기"
    assert w[0]["전환일"] == "2024-12-19" and w[0]["해당건수"] == 1
    assert "그대로 인용하지" in w[0]["지시"]


def test_no_warning_when_document_is_after_turning_point():
    items = [{"제목": "통상임금 재직조건 판정", "일자": "2025-06-01"}]
    info = vintage.annotate(items, keyword="통상임금", today=TODAY)
    assert "시점주의" not in info
    assert info["시점범위"] == "2025-06-01"


def test_keyword_alone_can_trigger_match():
    """검색어에 주제어가 있으면 부가어 없이도 매칭된다."""
    info = vintage.annotate([{"제목": "연차 관련 구제신청", "일자": "2019-01-01"}],
                            keyword="연차 미사용수당", today=TODAY)
    assert any(w["쟁점"].startswith("1년 기간제 연차") for w in info["시점주의"])


def test_supporting_word_alone_does_not_warn():
    """'동의'·'퇴직' 같은 흔한 부가어만으로는 경고가 뜨면 안 된다 (2026-09-04 실측 오탐)."""
    items = [{"제목": "재직조건부 정기상여금 지급제외 문의", "발췌": "노사 동의를 받았는지",
              "일자": "2023-07-21"}]
    info = vintage.annotate(items, keyword="통상임금 재직조건", today=TODAY)
    쟁점들 = {w["쟁점"] for w in info.get("시점주의", [])}
    assert "통상임금 고정성 폐기" in 쟁점들            # 주제어 '통상임금'이 검색어에 있다
    assert not any("취업규칙" in x for x in 쟁점들)    # '동의'만으로는 걸리지 않는다


def test_topic_word_without_supporting_word_and_without_keyword():
    """주제어가 문서에만 있고 부가어도 검색어 매칭도 없으면 경고하지 않는다."""
    info = vintage.annotate([{"제목": "취업규칙 신고 절차 안내", "일자": "2015-01-01"}],
                            keyword="신고", today=TODAY)
    assert "시점주의" not in info


def test_unrelated_old_document_gets_generic_note_only():
    info = vintage.annotate([{"제목": "산업안전 보호구 지급", "일자": "2010-01-01"}],
                            keyword="보호구", today=TODAY)
    assert "시점주의" not in info
    assert "5년 이상 지난 자료" in info["시점참고"]


def test_recent_unrelated_document_gets_nothing():
    info = vintage.annotate([{"제목": "보호구 지급", "일자": "2026-01-01"}],
                            keyword="보호구", today=TODAY)
    assert "시점주의" not in info and "시점참고" not in info
    assert info["시점범위"] == "2026-01-01"


def test_undated_documents_are_counted_and_flagged():
    info = vintage.annotate([{"제목": "요양불승인처분취소"}, {"제목": "x", "일자": "2026-01-01"}],
                            today=TODAY)
    assert info["일자미상"] == 1 and "일자를 알 수 없는" in info["시점참고"]


def test_future_turning_point_is_not_applied():
    """아직 오지 않은 전환점으로 과거 자료를 경고하면 안 된다."""
    items = [{"제목": "취업규칙 불이익변경 동의", "일자": "2024-01-01"}]
    early = vintage.annotate(items, keyword="취업규칙 불이익변경", today="2023-01-01")
    쟁점들 = {w["쟁점"] for w in early.get("시점주의", [])}
    assert not any("동의 방식" in x for x in 쟁점들)


def test_annotate_empty_and_non_dict_items():
    assert vintage.annotate([]) == {}
    assert vintage.annotate(["문자열", None]) == {}


def test_apply_to_merges_multiple_item_containers():
    res = {"고용노동부_행정해석": [{"안건명": "연차 산정", "해석일자": "2018.04.10"}],
           "법제처_법령해석례": [{"안건명": "연차", "회신일자": "2019.02.01"}]}
    vintage.apply_to(res, keyword="연차")
    assert res["시점범위"] == "2018-04-10 ~ 2019-02-01"
    assert res["시점주의"][0]["해당건수"] == 2


def test_apply_to_is_safe_on_odd_payloads():
    assert vintage.apply_to({"status": "NOT_FOUND", "items": []}) == {"status": "NOT_FOUND", "items": []}
    assert vintage.apply_to({"items": "목록아님"})["items"] == "목록아님"


def test_turning_point_table_is_well_formed():
    seen = set()
    for 전환일, 쟁점, 주제어들, 부가어들, 설명 in LC.DOCTRINE_TURNING_POINTS:
        assert vintage.iso_date(전환일) == 전환일, 전환일
        assert 쟁점 and 설명 and 주제어들 and 부가어들
        # 주제어는 그 쟁점을 특정할 만큼 구체적이어야 한다 — 노무 문서에 흔히 나오는
        # 일반어를 주제어로 두면 엉뚱한 경고가 뜬다
        너무_흔함 = {"동의", "퇴직", "임금", "근로", "휴가", "해고", "1년", "발생", "지급", "신고"}
        assert all(len(k) >= 2 and k not in 너무_흔함 for k in 주제어들), 주제어들
        assert (전환일, 쟁점) not in seen
        seen.add((전환일, 쟁점))
