# -*- coding: utf-8 -*-
"""test_law_committee.py — law.go.kr 위원회 결정문·행정심판례 클라이언트 + admrul 확장 검색

픽스처(tests/fixtures/*.xml)는 2026-09-03 실호출 응답에서 OC만 'test'로 바꾼 것.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import law_committee  # noqa: E402
import law_go_kr  # noqa: E402
from law_committee import ALIASES, TARGETS, CommitteeClient, resolve_target  # noqa: E402
from law_go_kr import LawInvalidInput, LawNotFound  # noqa: E402

FX = PROJECT_ROOT / "tests" / "fixtures"


def _fx(name):
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture
def fake_get(monkeypatch):
    """endpoint·target·ID로 픽스처를 고른다. 호출 파라미터를 기록해 검증에 쓴다."""
    calls = []

    def _get(endpoint, **params):
        calls.append((endpoint, params))
        tg = params["target"]
        if endpoint == "lawService.do":
            if str(params.get("ID")) == "999999999":
                return "<Law>일치하는 결정문이 없습니다.  제목을 확인하여 주십시오.</Law>"
            return _fx(f"{tg}_body.xml")
        if params.get("query") == "zzqq":
            return _fx("nlrc_list_empty.xml")
        return _fx(f"{tg}_list.xml")

    monkeypatch.setattr(law_committee, "_get", _get)
    monkeypatch.setattr(law_go_kr, "_get", _get)
    return calls


def test_resolve_target_aliases():
    assert resolve_target("노동위원회") == "nlrc"
    assert resolve_target("산재재심사위") == "iaciac"
    assert resolve_target("행정심판") == "decc"
    assert resolve_target("eiac") == "eiac"
    with pytest.raises(LawInvalidInput):
        resolve_target("헌법재판소")
    assert set(ALIASES.values()) <= set(TARGETS)


def test_nlrc_list_parsing(fake_get):
    r = CommitteeClient().search("노동위원회", "해고", display=3, sort="ddes")
    assert r["target"] == "nlrc" and r["total"] == 39878
    it = r["items"][0]
    assert it["일련번호"].isdigit() and it["제목"].endswith("구제신청")
    assert it["일자"] == it["등록일"]
    assert "상세링크" not in str(it)                        # OC가 박힌 링크는 노출 금지
    ep, p = fake_get[0]
    assert p["sort"] == "ddes" and p["search"] == 1 and p["display"] == 3


def test_nlrc_body_fields(fake_get):
    b = CommitteeClient().get("nlrc", "15257")
    assert b["자료구분"] == "부당해고" and b["담당부서"] == "서울지방노동위원회"
    assert b["판정결과"] == "기각" and "자진 퇴사" in b["판정사항"]
    assert b["제목"] == b["판정요지"][:0] + b["제목"]      # 제목 키가 보장된다
    assert "내용" not in b                                    # 빈 필드는 빠진다


def test_eiac_iaciac_decc_bodies(fake_get):
    c = CommitteeClient()
    e = c.get("고용보험심사위원회", "12687")
    assert e["사건번호"] == "2023재결 제44호" and e["주문"].startswith("피청구인이")
    assert "<span" not in e["주문"]                           # HTML 태그 제거
    i = c.get("산재재심사위원회", "7213")
    assert i["사건소분류"] == "휴게시간 중의 사고" and i["원처분기관"].startswith("근로복지공단")
    assert i["이유"].startswith("1. 사건 개요")
    d = c.get("행정심판", "272607")
    assert d["재결청"] == "국민권익위원회" and d["주문"] == "청구인의 청구를 기각한다."
    assert d["이유"] and d["제목"] == d["사건명"]


def test_body_truncation_and_not_found(fake_get):
    c = CommitteeClient()
    b = c.get("iaciac", "7213", max_chars=50)
    assert len(b["이유"]) == 50 and any(x.startswith("이유:") for x in b["잘림"])
    with pytest.raises(LawNotFound):
        c.get("nlrc", "999999999")
    with pytest.raises(LawInvalidInput):
        c.get("nlrc", "abc")


def test_list_variants(fake_get):
    c = CommitteeClient()
    assert c.search("eiac", "", display=3)["items"][0]["의결일자"] == "2023.06.21"
    assert c.search("iaciac", "")["items"][0]["제목"].startswith("2022재결")
    d = c.search("decc", "산재", date_from="20250101", date_to="20251231")
    assert d["items"][0]["재결청"] == "국민권익위원회"
    assert fake_get[-1][1]["rslYd"] == "20250101~20251231"     # decc는 서버측 필터
    assert c.search("nlrc", "zzqq")["items"] == []              # 빈 결과
    with pytest.raises(LawInvalidInput):
        c.search("nlrc", "해고", page=0)
    with pytest.raises(LawInvalidInput):
        c.search("nlrc", "해고", date_from="어제")


def test_client_side_date_filter_for_non_decc(fake_get):
    r = CommitteeClient().search("nlrc", "해고", date_from="20300101")
    assert r["items"] == [] and r["total"] == 39878


def test_iter_list_stops_at_total(fake_get):
    # 픽스처 total=118, 페이지당 100 → 2페이지에서 멈춘다 (픽스처가 같은 3건을 되돌려주므로 6행)
    rows = list(CommitteeClient().iter_list("eiac", "", display=100))
    assert len(rows) == 6 and rows[0]["일련번호"] == "12687"
    assert [p["page"] for _, p in fake_get] == [1, 2]


# ---------------------------------------------------------------------------
# 행정규칙(admrul) 확장 검색
# ---------------------------------------------------------------------------

def test_admrul_search_filters(fake_get):
    law = law_go_kr.LawGoKrClient()
    r = law.search_admin_rules("취업규칙", org="고용노동부", kind="예규", sort="ddes")
    assert r["total"] == 1 and r["items"][0]["행정규칙명"] == "취업규칙 심사요령"
    assert r["items"][0]["종류"] == "예규" and r["items"][0]["현행연혁"] == "현행"
    p = fake_get[-1][1]
    assert p["org"] == "1492000" and p["knd"] == "2" and p["nw"] == 1 and p["sort"] == "ddes"
    with pytest.raises(LawInvalidInput):
        law.search_admin_rules("x", org="없는부처")
    with pytest.raises(LawInvalidInput):
        law.search_admin_rules("x", kind="법률")


def test_admrul_search_all_departments(fake_get):
    law = law_go_kr.LawGoKrClient()
    law.search_admin_rules("", org="", current_only=False, search_body=True)
    p = fake_get[-1][1]
    assert "org" not in p or p["org"] == ""
    assert p["nw"] == 2 and p["search"] == 2


@pytest.mark.live
def test_live_nlrc_and_admrul():
    c = CommitteeClient()
    r = c.search("노동위원회", "해고", display=2, sort="ddes")
    assert r["total"] > 30000 and r["items"]
    b = c.get("노동위원회", r["items"][0]["일련번호"])
    assert "판정요지" in b or "판정사항" in b
    a = law_go_kr.LawGoKrClient().search_admin_rules("취업규칙", org="고용노동부")
    assert any("심사요령" in i["행정규칙명"] for i in a["items"])
