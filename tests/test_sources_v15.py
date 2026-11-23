# -*- coding: utf-8 -*-
"""test_sources_v15.py — v1.5 오프라인 테스트: 근로복지공단 심사결정서·질병판정서, 중노위 판정요지 파일"""
import csv
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import archive  # noqa: E402
import comwel  # noqa: E402
import ingest_archive as IA  # noqa: E402

SIMSA_BODY = ("？ 주문 청구인의 심사청구를 기각한다. 청구취지 청구인은 원처분기관이 2017. 7. 3. 행한 장해등급 "
              "결정처분의 취소를 구한다. 이 유 1. 처분내용 … 따라서 이에 주문과 같이 심사결정 합니다. "
              "2017년 11월 일 근 로 복 지 공 단 이 사 장 ※ 이 심사결정에 이의가 있을 때에는 …")
JIL_BODY = ("주문 신청인 ○○○가 요양급여 신청한 상병 ‘무후각증’은 산업재해보상보험법상 업무상 질병으로 "
            "인정한다. 청구취지 ○○ (기타 개인정보 생략)(2015.04.14.)에 따른 판정 요청 신청내용 신청인은 "
            "1998년경부터 2014. 10월까지 도장작업을 수행 …")


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(archive._SCHEMA)
    return c


# ---------------------------------------------------------------------------
# 일자 추출 — 원천에 일자 필드가 없다
# ---------------------------------------------------------------------------

def test_decision_date_simsa_month():
    assert comwel.decision_date("심사결정서", "20170006001", SIMSA_BODY) == ("2017-11-01", "결정연월")


def test_decision_date_jilbyeong_request_date():
    assert comwel.decision_date("질병판정서", "240020150000943", JIL_BODY) == ("2015-04-14", "판정요청일")


def test_decision_date_falls_back_to_case_year():
    assert comwel.decision_date("심사결정서", "20170006001", "주문 기각한다.") == ("2017-01-01", "사건번호 접수연도")
    # 질병판정 번호는 5~8자리가 연도
    assert comwel.decision_date("질병판정서", "440020190001018", "주문 인정한다.") == ("2019-01-01", "사건번호 접수연도")
    assert comwel.decision_date("질병판정서", "", "") == ("", "")


def test_decision_date_jilbyeong_review_request_field():
    body = "청구취지 지사:○○, 접수번호:(기타 개인정보 생략), 심의의뢰처리일자:2019.09.30 신청내용 …"
    assert comwel.decision_date("질병판정서", "340020190002319", body) == ("2019-09-30", "판정요청일")
    masked = "청구취지 (기타 개인정보 생략)에 따른 업무상질병 인정 여부 판정 요청 신청내용 2018.5.16. …"
    assert comwel.decision_date("질병판정서", "340020180002154", masked) == ("2018-01-01", "사건번호 접수연도")


def test_decision_date_rejects_far_away_paren_date():
    body = "청구취지 ○○ (2008.03.23.)에 따른 판정 요청"           # 접수 2015년인데 2008년 날짜
    assert comwel.decision_date("질병판정서", "240020150000943", body) == ("2015-01-01", "사건번호 접수연도")


def test_decision_date_rejects_impossible_dates():
    body = "청구취지 ○○ (2015.13.40.)에 따른 판정 요청"
    assert comwel.decision_date("질병판정서", "240020150000943", body) == ("2015-01-01", "사건번호 접수연도")


def test_date_labels_do_not_look_exact():
    assert comwel.date_label("2017-11-01", "결정연월") == "2017년 11월 (결정연월 — 일 미공개)"
    assert "판정일은 그 이후" in comwel.date_label("2015-04-14", "판정요청일")
    assert comwel.date_label("", "") == "미상"


def test_archive_date_fields_for_new_kinds():
    c = _mem()
    archive.upsert(c, "simsa", "1", title="t", doc_date="2017-11-01", date_kind="결정연월")
    archive.upsert(c, "jilbyeong", "2", title="t", doc_date="2015-04-14", date_kind="판정요청일")
    a = archive.get(c, "simsa", "1")
    b = archive.get(c, "jilbyeong", "2")
    assert a["일자"].startswith("2017년 11월") and a["일자근거"] == "결정연월"
    assert b["일자"].startswith("2015-04-14") and b["일자근거"] == "판정요청일"


def test_order_line():
    assert comwel.order_line(SIMSA_BODY) == "청구인의 심사청구를 기각한다."
    assert comwel.order_line(JIL_BODY).endswith("업무상 질병으로 인정한다.")


# ---------------------------------------------------------------------------
# 클라이언트 — 필터는 소문자 kinda/kindb/kindc
# ---------------------------------------------------------------------------

def _xml(items):
    blocks = "".join(
        "<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in it.items()) + "</item>" for it in items)
    return (f"<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>"
            f"<body><items>{blocks}</items><numOfRows>10</numOfRows><pageNo>1</pageNo>"
            f"<totalCount>{len(items)}</totalCount></body></response>")


def test_decision_client_lowercase_filters_and_mapping(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "k")
    calls = []

    class _R:
        status_code = 200

        def __init__(self, t):
            self.text = t

    def _get(url, params=None, **kw):
        calls.append((url, dict(params)))
        return _R(_xml([{"accnum": "240020150000943", "kinda": "인정", "kindb": "제조관련 단순 종사원",
                         "kindc": "호흡기질환(천식 포함)", "title": "플라스틱가공제품제조업",
                         "noncontent": JIL_BODY}]))

    monkeypatch.setattr(comwel.requests, "get", _get)
    r = comwel.ComwelDecisionClient("질병판정").search(a="인정", c="호흡기질환(천식 포함)", rows=500)
    url, p = calls[-1]
    assert url.endswith("/jilbyeongPstateInfoService/getJilbyeongResultNaeyongPstate")
    assert p["kinda"] == "인정" and p["kindc"] == "호흡기질환(천식 포함)"
    assert "kindA" not in p and "kindb" not in p and p["numOfRows"] == 100
    it = r["items"][0]
    assert it["판정번호"] == "240020150000943" and it["업종"] == "플라스틱가공제품제조업"
    assert it["판정결과"] == "인정" and it["직종"] == "제조관련 단순 종사원"
    assert it["일자_ISO"] == "2015-04-14" and it["일자근거"] == "판정요청일"
    assert it["판정서"].startswith("주문")


def test_resolve_kind():
    assert comwel.resolve_kind("심사결정서") == "심사결정서"
    assert comwel.resolve_kind("질판위") == "질병판정서"
    with pytest.raises(ValueError):
        comwel.resolve_kind("판례")


# ---------------------------------------------------------------------------
# 자료원 별칭 — 튜플로 펼침
# ---------------------------------------------------------------------------

def test_aliases_expand():
    assert archive.resolve_sources("노동위원회") == ["nlrc", "nlrcsum"]
    assert archive.resolve_sources("근로복지공단") == ["comwel", "simsa", "jilbyeong"]
    assert archive.resolve_sources("산재") == ["comwel", "simsa", "jilbyeong", "iaciac"]
    assert archive.resolve_sources("산재판례,질병판정서") == ["comwel", "jilbyeong"]
    assert archive.resolve_sources("심사결정서") == ["simsa"]


def test_new_sources_in_refresh_order():
    order = IA.SOURCE_ORDER
    assert {"simsa", "jilbyeong", "nlrcsum"} <= set(order)
    assert order.index("nlrcsum") == order.index("nlrc") + 1      # nlrc 직후여야 중복 정리가 된다
    assert set(order) == set(archive.SOURCES)


# ---------------------------------------------------------------------------
# 적재 — 심사결정서 · 질병판정서
# ---------------------------------------------------------------------------

def test_ingest_comwel_decision(monkeypatch):
    items = [{"사건번호": "20170006001", "사건명": "장해등급 결정처분 취소 청구", "결정결과": "기각",
              "청구유형": "장해급여지급", "쟁점구분": "의학적 판단차이", "주문": "청구인의 심사청구를 기각한다.",
              "결정문": SIMSA_BODY, "일자_ISO": "2017-11-01", "일자근거": "결정연월"}]

    class _Fake:
        spec = comwel.DECISION_KINDS["심사결정서"]

        def __init__(self, kind):
            pass

        def search(self, page=1, rows=100, max_chars=0):
            return {"total": 1, "items": items if page == 1 else []}

    monkeypatch.setattr(comwel, "ComwelDecisionClient", _Fake)
    c = _mem()
    assert IA.Runner(c, interval=0).ingest_comwel_decision("simsa") == (1, 0)
    d = archive.get(c, "simsa", "20170006001")
    assert d["제목"] == "장해등급 결정처분 취소 청구" and d["요지"] == "청구인의 심사청구를 기각한다."
    assert d["구분"] == "장해급여지급 / 의학적 판단차이" and d["추가정보"]["결정결과"] == "기각"
    assert d["일자근거"] == "결정연월"
    assert IA.Runner(c, interval=0).ingest_comwel_decision("simsa") == (0, 0)     # 증분: 다시 담지 않음
    res = archive.search(c, "장해등급", sources="근로복지공단")
    assert res["total"] == 1


# ---------------------------------------------------------------------------
# 중노위 판정요지 파일
# ---------------------------------------------------------------------------

def _zip(tmp_path, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(IA._NLRC_FILE_COLS))
    w.writeheader()
    w.writerows(rows)
    p = tmp_path / "FILE_1.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("판정요지.csv", ("﻿" + buf.getvalue()).encode("utf-8"))
    return p


YOJI_A = "해고의 ‘긴박한 경영상 필요’가 있는지 여부는 사용자 전체의 재정이나 경영사정을 기준으로 판단하여야 한다"
YOJI_B = "무기계약직 전환기대권은 인정되나 무기계약직 전환을 거절하는 데에는 합리적인 이유가 있음"


def test_nlrc_doc_no():
    assert IA.nlrc_doc_no("서울지방노동위원회", "2015부해2684") == "서울2015부해2684"
    assert IA.nlrc_doc_no("중앙노동위원회", "2015부해 998") == "중앙2015부해998"


def test_ingest_nlrcsum_enrich_insert_and_dedupe(tmp_path, monkeypatch):
    c = _mem()
    # law.go.kr 결정문 — 사건번호가 가려져 있다
    archive.upsert(c, "nlrc", "15257", title="○ ○ ○ 부당해고 구제신청", doc_no="2015BUHAOOO",
                   org="서울지방노동위원회", doc_date="2015-06-01", body=YOJI_A + "\n\n내용…")
    rows = [
        {"구분": "○ ○ ○ 부당해고 구제신청", "관할위원회": "서울지방노동위원회", "사건번호": "2015부해2684",
         "판정사항": "전부인정", "판정요지": YOJI_A.replace("‘", "'")},          # 따옴표가 달라도 일치
        {"구분": "○ ○ ○ 부당해고 구제신청", "관할위원회": "충남지방노동위원회", "사건번호": "2025부해9078",
         "판정사항": "기각", "판정요지": YOJI_B},                                # nlrc에 없음 → nlrcsum
    ]
    path = _zip(tmp_path, rows)
    monkeypatch.setattr(IA.Runner, "_nlrc_file_path", lambda self: path)

    assert IA.Runner(c, interval=0).ingest_nlrcsum() == (1, 0)
    assert archive.get(c, "nlrc", "15257")["문서번호"] == "서울2015부해2684"
    new = archive.get(c, "nlrcsum", "충남2025부해9078")
    assert new["일자근거"] == "사건번호 접수연도" and new["추가정보"]["판정결과"] == "기각"
    assert new["제목"].startswith("부당해고 구제신청 — 충남지방노동위원회 2025부해9078")
    # 바뀐 문서번호로 찾을 수 있다 (색인 갱신 확인) — verify_citations의 로컬 경로
    assert archive.find_by_doc_no(c, "2015부해2684", sources=["nlrc", "nlrcsum"])
    assert archive.find_by_doc_no(c, "충남2025부해9078", sources=["nlrc", "nlrcsum"])

    # 다음 달: law.go.kr에 충남 사건이 들어왔다 → nlrcsum 쪽은 지우고 nlrc에 번호를 붙인다
    archive.upsert(c, "nlrc", "99999", title="부당해고", doc_no="2025BUHAOOO",
                   org="충남지방노동위원회", body=YOJI_B)
    c.commit()
    assert IA.Runner(c, interval=0).ingest_nlrcsum() == (0, 0)
    assert archive.get(c, "nlrcsum", "충남2025부해9078") is None
    assert archive.get(c, "nlrc", "99999")["문서번호"] == "충남2025부해9078"
    assert archive.search(c, "무기계약직 전환기대권", sources="노동위원회")["total"] == 1


def test_ingest_nlrcsum_ambiguous_file_key_not_enriched(tmp_path, monkeypatch):
    c = _mem()
    archive.upsert(c, "nlrc", "1", title="t", doc_no="2020BUHAOOO", org="중앙노동위원회", body=YOJI_A)
    rows = [{"구분": "재심", "관할위원회": "중앙노동위원회", "사건번호": n, "판정사항": "기각", "판정요지": YOJI_A}
            for n in ("2020부해1", "2020부해2")]                               # 병합 사건 — 같은 요지
    path = _zip(tmp_path, rows)
    monkeypatch.setattr(IA.Runner, "_nlrc_file_path", lambda self: path)
    assert IA.Runner(c, interval=0).ingest_nlrcsum() == (0, 0)
    assert archive.get(c, "nlrc", "1")["문서번호"] == "2020BUHAOOO"               # 어느 번호인지 몰라 그대로


def test_nlrc_file_rows_rejects_changed_columns(tmp_path):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.csv", "열1,열2\n1,2\n".encode("utf-8"))
    with pytest.raises(IA.LawUpstreamError):
        IA.nlrc_file_rows(p)


def test_delete_and_set_doc_no_keep_index_consistent():
    c = _mem()
    archive.upsert(c, "nlrcsum", "a", title="연차휴가 판정", body="연차휴가 사용 촉진")
    assert archive.search(c, "연차휴가")["total"] == 1
    assert archive.set_doc_no(c, "nlrcsum", "a", "서울2020부해1")
    assert archive.set_doc_no(c, "nlrcsum", "a", "서울2020부해1") is False       # 그대로면 손대지 않음
    assert archive.search(c, "연차휴가")["total"] == 1
    assert archive.delete(c, "nlrcsum", "a") and archive.delete(c, "nlrcsum", "a") is False
    assert archive.search(c, "연차휴가")["total"] == 0
