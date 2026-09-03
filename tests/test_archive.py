# -*- coding: utf-8 -*-
"""test_archive.py — archive.py (SQLite+FTS5 사례 아카이브) 오프라인 테스트"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import archive  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(archive._SCHEMA)
    archive.upsert(c, "nlrc", "15257", title="○○○ 부당해고 구제신청", doc_no="2016부해OOO",
                   doc_date="2016.5.9.", category="부당해고", org="서울지방노동위원회",
                   summary="근로자가 자진 퇴사한 것으로 해고가 존재하지 않는다고 판정한 사례",
                   body="사용자가 근로자를 해고하였다고 보기는 어려움.", extra={"판정결과": "기각"})
    archive.upsert(c, "moel", "17076", title="연차유급휴가 산정 방법", doc_no="근로기준정책과-3084",
                   doc_date="2021.05.11", org="고용노동부",
                   body="[질의요지]\n1년 미만 근로자의 연차휴가\n\n[회답]\n매월 개근 시 1일씩 11일")
    archive.upsert(c, "counsel", "202609030114400170611", title="아르바이트 급여 미지급", doc_no="104043",
                   doc_date="2026.09.03", body="[질의]\n급여를 안 줍니다\n\n[답변]\n임금체불 진정 가능")
    c.commit()
    return c


# ---------------------------------------------------------------------------
# 색인·검색어 변환
# ---------------------------------------------------------------------------

def test_ngrams_bigram_within_token():
    assert archive.ngrams("부당해고를") == "부당 당해 해고 고를"
    assert archive.ngrams("해고 A") == "해고 a"                 # 1글자 어절은 그대로, 소문자화
    assert archive.ngrams("2020다247190").startswith("20 02 20 0다 다2")


def test_build_match_phrases_and_tokens():
    match, tokens = archive.build_match("부당해고 징계 A")
    assert match == '"부당 당해 해고" AND "징계"'
    assert tokens == ["부당해고", "징계", "a"]
    assert archive.build_match("")[0] == ""


@pytest.mark.parametrize("raw,expected", [
    ("2016.5.9.", "2016-05-09"), ("2023.06.21", "2023-06-21"), ("20260626", "2026-06-26"),
    ("2026-09-02", "2026-09-02"), ("2026.13.01", ""), ("", ""), ("없음", ""),
])
def test_norm_date(raw, expected):
    assert archive.norm_date(raw) == expected


def test_resolve_sources_aliases_and_errors():
    assert archive.resolve_sources("노동위원회,행정해석") == ["nlrc", "moel"]
    assert archive.resolve_sources(["nlrc", "nlrc"]) == ["nlrc"]
    assert archive.resolve_sources("") == []
    with pytest.raises(ValueError):
        archive.resolve_sources("없는자료원")


# ---------------------------------------------------------------------------
# 적재·조회
# ---------------------------------------------------------------------------

def test_search_two_char_keyword_hits_body(conn):
    r = archive.search(conn, "해고")
    assert r["total"] == 1 and r["items"][0]["자료원"] == "nlrc"
    assert "발췌" in r["items"][0] and "해고" in r["items"][0]["발췌"]


def test_search_multi_token_is_and(conn):
    assert archive.search(conn, "연차 휴가")["total"] == 1
    assert archive.search(conn, "연차 해고")["total"] == 0


def test_search_doc_no_and_source_filter(conn):
    assert archive.search(conn, "근로기준정책과-3084")["items"][0]["문서번호"] == "근로기준정책과-3084"
    assert archive.search(conn, "급여", sources="상담")["total"] == 1
    assert archive.search(conn, "급여", sources="노동위원회")["total"] == 0


def test_search_date_filter_and_latest_first(conn):
    r = archive.search(conn, "근로", date_from="20200101", latest_first=True)
    dates = [i["일자"] for i in r["items"]]
    assert dates == sorted(dates, reverse=True) and all(d >= "2020-01-01" for d in dates)
    with pytest.raises(ValueError):
        archive.search(conn, "근로", date_from="어제")


def test_search_single_char_falls_back_to_like(conn):
    r = archive.search(conn, "A")
    assert r["total"] == 0
    r = archive.search(conn, "차")                             # 1글자 한글 → LIKE 경로
    assert r["total"] == 1 and r["items"][0]["자료원"] == "moel"
    with pytest.raises(ValueError):                             # 색인 대상 문자가 없는 검색어
        archive.search(conn, "○○○")


def test_search_empty_keyword_rejected(conn):
    with pytest.raises(ValueError):
        archive.search(conn, "   ")


def test_upsert_updates_and_reindexes(conn):
    assert archive.upsert(conn, "nlrc", "15257", title="갱신 제목", doc_no="2016부해OOO",
                          body="복직 명령") is False
    conn.commit()
    assert archive.search(conn, "복직")["total"] == 1
    assert archive.search(conn, "자진")["total"] == 0          # 옛 색인이 지워졌다
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 3


def test_get_and_find_by_doc_no(conn):
    d = archive.get(conn, "노동위원회", "15257")
    assert d["본문"].startswith("사용자가") and d["추가정보"] == {"판정결과": "기각"}
    assert archive.get(conn, "nlrc", "없음") is None
    hits = archive.find_by_doc_no(conn, "정책과-3084")
    assert hits and hits[0]["문서번호"] == "근로기준정책과-3084"
    assert archive.find_by_doc_no(conn, "정책과-3084", sources="nlrc") == []


def test_get_truncates_long_body(conn):
    archive.upsert(conn, "moel", "L", title="장문", body="가" * 100)
    d = archive.get(conn, "moel", "L", max_chars=10)
    assert len(d["본문"]) == 10 and "잘림" in d


def test_stats_and_state(conn):
    archive.set_state(conn, "nlrc", "OK", added=3, note="1초")
    s = archive.stats(conn)
    assert s["총건수"] == 3
    assert s["자료원별"]["nlrc"]["건수"] == 1 and s["자료원별"]["nlrc"]["적재상태"] == "OK"
    assert any(x.startswith("comwel") for x in s["미적재자료원"])


def test_upsert_rejects_unknown_source_or_empty_id(conn):
    with pytest.raises(ValueError):
        archive.upsert(conn, "없음", "1", title="x")
    with pytest.raises(ValueError):
        archive.upsert(conn, "nlrc", "  ", title="x")


def test_open_db_readonly_requires_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LABOR_ARCHIVE_DB", str(tmp_path / "x.sqlite"))
    assert archive.exists_db() is False
    with pytest.raises(FileNotFoundError):
        archive.open_db(readonly=True)
    c = archive.open_db()
    archive.upsert(c, "nlrc", "1", title="t", body="해고")
    c.commit(); c.close()
    assert archive.exists_db() is True
    ro = archive.open_db(readonly=True)
    assert archive.search(ro, "해고")["total"] == 1
    ro.close()
