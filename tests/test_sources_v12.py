# -*- coding: utf-8 -*-
"""test_sources_v12.py — v1.2 신규 원천 파서: 빠른인터넷상담 · 질의회시집 PDF · 산재판례 API"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import comwel  # noqa: E402
import moel_fastcounsel as fc  # noqa: E402
import qna_pdf  # noqa: E402

FX = PROJECT_ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# 빠른인터넷상담 (moel.go.kr 화면 픽스처 — 2026-09-03 실응답 발췌)
# ---------------------------------------------------------------------------

def test_fastcounsel_list_parse():
    r = fc.parse_list_html((FX / "fastcounsel_list.html").read_text(encoding="utf-8"))
    assert r["total"] == 104053 and len(r["items"]) == 10
    it = r["items"][0]
    assert it["id"] == "202609030114400170611" and it["번호"] == "104043"
    assert it["등록일"] == "2026.09.03" and it["답변여부"] == "미완료"
    assert "아르바이트" in it["제목"]


def test_fastcounsel_view_parse():
    v = fc.parse_view_html((FX / "fastcounsel_view.html").read_text(encoding="utf-8"))
    assert v["질의"] == "무료노무상담 서비스가 있을까요?"
    assert v["답변"].startswith("귀하께서 근로기준법등")


def test_fastcounsel_parse_errors():
    with pytest.raises(fc.FastCounselParseError):
        fc.parse_view_html("<html><body>개편됨</body></html>")
    with pytest.raises(fc.FastCounselParseError):
        fc.parse_list_html("<html><body>아무것도 없음</body></html>")


def test_fastcounsel_client_params(monkeypatch):
    calls = []

    class _R:
        text = (FX / "fastcounsel_list.html").read_text(encoding="utf-8")

        def raise_for_status(self):
            pass

    def _get(url, params=None, timeout=None):
        calls.append((url, params))
        return _R()

    c = fc.FastCounselClient(min_interval=0)
    monkeypatch.setattr(c._s, "get", _get)
    c.list(page=2, unit=30, keyword="연차", field="답변")
    assert calls[-1][1] == {"pageIndex": 2, "pageUnit": 30, "searchField": "2", "searchText": "연차"}
    c.list(page=1, unit=99)                                   # 허용되지 않는 unit → 50
    assert calls[-1][1]["pageUnit"] == 50 and "searchText" not in calls[-1][1]
    with pytest.raises(ValueError):
        c.list(page=0)
    with pytest.raises(ValueError):
        c.get("abc")


# ---------------------------------------------------------------------------
# 근로기준법 질의회시집 PDF (추출 텍스트 형상을 흉내 낸 합성 페이지)
# ---------------------------------------------------------------------------

_PAGES = [
    "",                                                     # 표지(이미지)
    "38 / 근로기준법 질의회시집\n"
    "제1장 총 칙\n"
    "1 근로자\n"
    "「군형법」상  군인의  근로자성  여부\n"
    "「군형법」 제2조제2항의 군인이 근로자인지\n"
    "군인은 「근로기준법」상 근로자로 보기 어려움.\n"
    "(근로기준정책과-1384, 2021.5.11.)\n"
    "센터  운영  수탁기관이  임명한  센터장의  근로자성  여부\n"
    "A시청이 위탁한 센터장의 근로자성\n",
    "제1장 총칙 / 39\n"
    "사용종속관계가 인정되면 근로자에 해당함.\n"
    "(근로기준정책과-809, 2021.3.16.)\n"
    "제3장 임금 / 120\n"
    "7 임금 연대 책임\n"
    "휴업수당의  「근로기준법」  제44조  적용여부\n"
    "도급인의 귀책사유로 휴업한 경우\n"
    "제44조가 적용됨.\n"
    "(근기 01254-751, 1993.4.27.)\n",
]


def test_qna_pdf_entries():
    es = qna_pdf.parse_entries(_PAGES)
    assert [e["doc_no"] for e in es] == ["근로기준정책과-1384", "근로기준정책과-809", "근기 01254-751"]
    assert es[0]["date"] == "2021-05-11" and es[2]["date"] == "1993-04-27"
    assert es[0]["title"] == "「군형법」상 군인의 근로자성 여부"        # 장·절 머리글 제거, 공백 정리
    assert es[0]["chapter"] == "" or es[0]["chapter"].startswith("제1장")
    assert es[1]["chapter"] == "제1장 총칙" and es[1]["body"].startswith("A시청이")
    assert "근로기준법 질의회시집" not in es[1]["body"]                 # 쪽 머리글 제거
    assert es[2]["chapter"] == "제3장 임금" and es[2]["title"].startswith("휴업수당의")
    assert es[2]["page"] == 3


def test_qna_pdf_real_file_if_present():
    pdf = PROJECT_ROOT / "data" / "근로기준법_질의회시집_2018-2023.pdf"
    if not pdf.exists():
        pytest.skip("원본 PDF 미보유 (ingest_archive.py qnabook 실행 시 내려받음)")
    es = qna_pdf.parse_pdf(pdf)
    assert len(es) >= 380
    assert all(e["doc_no"] and e["body"] for e in es)


# ---------------------------------------------------------------------------
# 근로복지공단 산재판례 API — 오류 봉투와 항목 파싱 (성공 응답은 스웨거 스키마 기준 합성)
# ---------------------------------------------------------------------------

_ERR = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>
<returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg><returnReasonCode>30</returnReasonCode>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""
_OK = """<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
<body><items><item><accnum>2019구단12345</accnum><courtname>서울행정법원</courtname><kinda>기각</kinda>
<kindb>요양</kindb><kindc>업무상 질병</kindc><title>요양불승인처분취소</title>
<noncontent><![CDATA[1. 처분의 경위<br/>원고는 …]]></noncontent></item></items>
<numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>1234</totalCount></body></response>"""


def test_comwel_key_is_url_decoded(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "abc%2Bdef%3D%3D")      # 포털의 '인코딩 키'
    assert comwel.api_key() == "abc+def=="
    monkeypatch.setenv("DATA_GO_KR_KEY", " abc+def== ")            # '디코딩 키'는 그대로
    assert comwel.api_key() == "abc+def=="


def test_comwel_requires_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    with pytest.raises(comwel.ComwelAuthError):
        comwel.ComwelClient().codes()


def test_comwel_error_envelope(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "k")

    class _R:
        status_code = 403
        text = _ERR

    monkeypatch.setattr(comwel.requests, "get", lambda *a, **k: _R())
    with pytest.raises(comwel.ComwelAuthError) as ei:
        comwel.ComwelClient().search()
    assert "활용신청" in str(ei.value)


def test_comwel_success_parse(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "k")
    calls = []

    class _R:
        status_code = 200
        text = _OK

    def _get(url, params=None, **k):
        calls.append((url, params))
        return _R()

    monkeypatch.setattr(comwel.requests, "get", _get)
    r = comwel.ComwelClient().search(result_type="기각", case_type="요양", page=2, rows=20, max_chars=8)
    assert r["total"] == 1234 and r["page"] == 2
    it = r["items"][0]
    assert it["사건번호"] == "2019구단12345" and it["법원명"] == "서울행정법원"
    assert it["판결문"] == "1. 처분의 경" and "잘림" in it
    url, p = calls[-1]
    assert url.endswith("/getSjbPrecedentNaeyongPstate")
    assert p["kindA"] == "기각" and p["kindB"] == "요양" and "kindC" not in p
    assert p["pageNo"] == 2 and p["numOfRows"] == 20
    with pytest.raises(ValueError):
        comwel.ComwelClient().search(page=0)
