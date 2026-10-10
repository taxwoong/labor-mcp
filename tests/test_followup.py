# -*- coding: utf-8 -*-
"""
test_followup.py — v1.4.0 오프라인 테스트
  ① 개명된 법령의 연혁: 같은 법령ID의 개명 전 시행본 포함 (LID 검색)
     (2026-10-10 실측: 남녀고용평등법 1988~2007 시행본이 이름 검색 연혁에서 빠져 1990년 기준 조회가 실패)
  ② 조문 원문의 메타데이터 줄·중복 항번호 제거

law.go.kr에는 접속하지 않는다 (_get을 가짜로 바꿔 끼움).

실행:
    python tests/test_followup.py
    python -m pytest tests/test_followup.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import law_go_kr  # noqa: E402


class _FakeGet:
    """law_go_kr._get을 잠시 바꿔 끼우고 호출 기록을 남긴다. handler(endpoint, **params) → XML 문자열."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __enter__(self):
        self.orig = law_go_kr._get

        def fake(endpoint, **params):
            self.calls.append((endpoint, params))
            return self.handler(endpoint, **params)

        law_go_kr._get = fake
        return self

    def __exit__(self, *exc):
        law_go_kr._get = self.orig
        return False


def _eflaw_xml(rows):
    return "<LawSearch>" + "".join(
        f"<law id='{i}'><법령명한글><![CDATA[{n}]]></법령명한글><법령ID>{lid}</법령ID>"
        f"<법령일련번호>{mst}</법령일련번호><시행일자>{eff}</시행일자><공포일자>{pub}</공포일자>"
        f"<공포번호>{no}</공포번호><제개정구분명>{kind}</제개정구분명></law>"
        for i, (n, lid, mst, eff, pub, no, kind) in enumerate(rows, 1)) + "</LawSearch>"


# 법령ID·MST는 시험용 가짜 값
OLD = [("남녀고용평등법", "900030", "5001", "19880401", "19871204", "03989", "제정"),
       ("남녀고용평등법", "900030", "5002", "19950804", "19950804", "04986", "일부개정")]
NEW = [("남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률", "900030", "5003", "20080622", "20071221", "08781", "전부개정"),
       ("남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률", "900030", "5004", "20261127", "20260526", "21400", "일부개정")]
OTHER = [("남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률 시행령", "900031", "5100", "20260101", "20260101", "36000", "일부개정")]
NAME = "남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률"


def _rows(tuples):
    """연혁 튜플 → law_history가 돌려주는 행(dict), 시행일 순."""
    keys = ("법령명", "법령ID", "MST", "시행일자", "공포일자", "공포번호", "제개정구분")
    return sorted((dict(zip(keys, t)) for t in tuples), key=lambda r: r["시행일자"])


def _history_handler(name_rows, lid_rows):
    def handler(endpoint, **p):
        if p.get("page", 1) != 1:
            return "<LawSearch></LawSearch>"
        if "LID" in p:
            return _eflaw_xml(lid_rows.get(p["LID"], []))
        return _eflaw_xml(name_rows)

    return handler


def test_history_adds_rows_from_before_the_rename():
    with _FakeGet(_history_handler(NEW + OTHER, {"900030": OLD + NEW})) as g:
        rows = law_go_kr.LawGoKrClient().law_history(NAME)
    assert [r["MST"] for r in rows] == ["5001", "5002", "5003", "5004"], "시행령 제외 + 개명 전 행 포함, 시행일 순"
    assert rows[0]["법령명"] == "남녀고용평등법"
    assert any(c[1].get("LID") == "900030" for c in g.calls)


def test_history_skips_lid_query_when_enactment_row_present():
    with _FakeGet(_history_handler(OLD + NEW + OTHER, {})) as g:
        rows = law_go_kr.LawGoKrClient().law_history(NAME)
    assert len(rows) == 4 and not any("LID" in c[1] for c in g.calls), "제정 행이 있으면 LID 왕복을 늘리지 않는다"
    assert len(g.calls) == 1, "한 쪽이 100건 미만이면 빈 쪽을 더 요청하지 않는다"


def test_history_with_law_id_uses_lid_directly_and_filters():
    with _FakeGet(_history_handler([], {"900030": OLD + NEW + OTHER})) as g:
        rows = law_go_kr.LawGoKrClient().law_history(NAME, law_id="900030")
    assert [r["MST"] for r in rows] == ["5001", "5002", "5003", "5004"], "다른 법령ID 행은 거른다"
    assert all("LID" in c[1] and "query" not in c[1] for c in g.calls), g.calls


def test_history_falls_back_to_name_when_lid_search_is_empty():
    with _FakeGet(_history_handler(NEW + OLD + OTHER, {})) as g:
        rows = law_go_kr.LawGoKrClient().law_history(NAME, law_id="900030")
    assert [r["MST"] for r in rows] == ["5001", "5002", "5003", "5004"]
    assert any("query" in c[1] for c in g.calls)


def test_repealed_and_reenacted_law_keeps_both_ids_without_extra_queries():
    """근로기준법처럼 폐지 후 재제정돼 이름이 같은 두 법령ID는 둘 다 제정 행이 있어 LID 왕복이 없다."""
    a = ("근로기준법", "900040", "6001", "19530809", "19530524", "00286", "제정")
    b = ("근로기준법", "900041", "6002", "19970313", "19970313", "05309", "제정")
    with _FakeGet(_history_handler([a, b], {})) as g:
        rows = law_go_kr.LawGoKrClient().law_history("근로기준법")
    assert {r["법령ID"] for r in rows} == {"900040", "900041"} and len(g.calls) == 1


def test_as_of_before_rename_returns_old_name_with_notice():
    c = law_go_kr.LawGoKrClient()
    c.law_history = lambda name, law_id="", **k: _rows(OLD + NEW)
    c.law_article = lambda mst, art, max_chars=6000, **k: {"조문": "제1조", "조문제목": "목적", "원문": "제1조 …"}
    c._check_promulgated = lambda art, *a, **k: art
    out = c.law_article_as_of(NAME, "20000101", "1")
    assert out["적용시행본"]["법령명"] == "남녀고용평등법" and out["적용시행본"]["MST"] == "5002"
    assert "남녀고용평등법" in out["주의"] and "같은 법령ID" in out["주의"], out.get("주의")
    now = c.law_article_as_of(NAME, "20270101", "1")
    assert "주의" not in now, now.get("주의")


def test_as_of_scheduled_future_rename_is_not_the_current_name():
    """개명이 시행 예정(미래 행)이어도 '현재 법령명'은 오늘 시행 중인 이름이다 — 오늘 기준 조회에 개명 안내가 붙으면 안 됨."""
    future = ("새이름법", "900030", "5005", "29990101", "29981201", "99999", "일부개정")
    c = law_go_kr.LawGoKrClient()
    c.law_history = lambda name, law_id="", **k: _rows(NEW + [future])
    c.law_article = lambda mst, art, max_chars=6000, **k: {"조문": "제1조", "조문제목": "t", "원문": "x"}
    c._check_promulgated = lambda art, *a, **k: art
    assert "주의" not in c.law_article_as_of(NAME, "20270101", "1")


# ---------------------------------------------------------------- ② 조문 원문 정리
BLOCK = (
    "<조문번호>101</조문번호>\n<조문여부>조문</조문여부>\n<조문제목><![CDATA[감독 기관]]></조문제목>\n"
    "<조문시행일자>20261008</조문시행일자>\n<조문제개정유형>일부개정</조문제개정유형>\n"
    "<조문이동이전></조문이동이전>\n<조문이동이후></조문이동이후>\n<조문변경여부>N</조문변경여부>\n"
    "<조문내용><![CDATA[제101조(감독 기관)]]></조문내용>\n"
    "<항>\n<항번호><![CDATA[①]]></항번호>\n<항내용><![CDATA[① 근로조건의 기준을 확보하기 위하여 근로감독관을 둔다. "
    "<개정 2010.6.4>]]></항내용>\n</항>\n"
    "<항>\n<항번호><![CDATA[②]]></항번호>\n<항내용><![CDATA[번호가 본문에 없는 항]]></항내용>\n</항>\n"
)


def test_block_text_drops_metadata_lines_and_duplicate_numbers():
    t = law_go_kr.LawGoKrClient._block_text(BLOCK)
    assert t.startswith("제101조(감독 기관)"), repr(t[:40])
    for junk in ("20261008", "일부개정", "000000", "\nN\n"):
        assert junk not in t, (junk, t)
    assert t.count("①") == 1
    assert "〈개정 2010.6.4〉" in t, "개정 주석은 보존"
    assert "②\n번호가 본문에 없는 항" in t.replace("\n\n", "\n")


def test_block_text_keeps_every_content_line():
    c = law_go_kr.LawGoKrClient
    t = "".join(c._block_text(BLOCK).split())
    for ln in c._article_content_lines(BLOCK):
        assert "".join(ln.split()) in t, ln


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
