# -*- coding: utf-8 -*-
"""
tests/test_sources_v13.py — v1.3 신규 원천 파서
건강보험분쟁조정위원회 재결례(simpan.go.kr) · 국민연금 (재)심사청구 결정사례(nps.or.kr)

픽스처는 2026-09-12 실응답에서 필요한 부분만 잘라 저장한 것이다.
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hidrc  # noqa: E402
import nps_review as npsrv  # noqa: E402

FX = PROJECT_ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# 건강보험분쟁조정위원회 (simpan.go.kr AJAX 응답)
# ---------------------------------------------------------------------------

def _hidrc_rows():
    data = json.loads((FX / "hidrc_list.json").read_text(encoding="utf-8"))
    items = []
    for row in data["list"]:
        sn = row.get("pdf_doc_atch_file_sn") or row.get("korn_doc_atch_file_sn")
        items.append({
            "사건번호": row.get("incdnt_no", ""),
            "사건명": row.get("incdnt_nm") or row.get("doc_ttl", ""),
            "재결일자": row.get("redc_ymd", ""),
            "재결결과": row.get("adjdc_result_nm", ""),
            "위원회": row.get("cmt_nm", ""),
            "첨부일련번호": str(sn) if sn else "",
        })
    return data["totCnt"], items


def test_hidrc_list_shape():
    total, items = _hidrc_rows()
    assert total == 68 and len(items) == hidrc.PAGE_SIZE
    it = items[0]
    assert it["위원회"] == "건강보험분쟁조정위원회"
    assert it["사건번호"] and it["재결일자"].count("-") == 2
    assert it["첨부일련번호"].isdigit()      # 없으면 본문 PDF를 못 받는다


def test_hidrc_committee_codes():
    """장기요양·국민연금 위원회 코드도 들고 있어야 한다 — 공개가 시작되면 바로 켤 수 있게."""
    assert hidrc.COMMITTEES["건강보험분쟁조정위원회"] == "40100009"
    assert set(hidrc.COMMITTEES) >= {"장기요양재심사위원회", "국민연금재심사위원회"}


def test_hidrc_unknown_committee_rejected():
    c = hidrc.HidrcClient()
    with pytest.raises(ValueError, match="알 수 없는 위원회"):
        c._params("없는위원회", 1)


def test_hidrc_csrf_token_is_fresh_base64():
    a, b = hidrc._csrf(), hidrc._csrf()
    import base64
    assert a != b and len(base64.b64decode(a)) == 32


def test_hidrc_split_sections_headings_inline():
    """PDF에서 뽑은 표제는 줄 첫머리에 오되 내용이 같은 줄에 이어붙는다.

    표제 뒤 줄바꿈을 요구하면 아무것도 못 자르고 전문만 돌아온다 (실제로 겪은 버그).
    """
    text = ("사건명 정산보험료부과처분취소청구\n"
            "재결결과 기각\n"
            "재결요지 국민건강보험법 제74조제1항 단서에 따라 보험료가 면제되지 않는다.\n"
            "주문 청구인의 청구를 기각한다.\n"
            "청구취지 처분을 취소한다.\n"
            "이유 1. 사건개요 가. 청구인은 직장가입자로서…\n")
    sec = hidrc.split_sections(text)
    assert set(sec) >= {"재결요지", "주문", "청구취지", "이유"}
    assert sec["주문"] == "청구인의 청구를 기각한다."
    assert sec["재결요지"].startswith("국민건강보험법 제74조")
    assert "사건명" in sec["머리"]


def test_hidrc_split_sections_ignores_out_of_order_repeats():
    """본문 안에서 '주문'이 다시 나와도 절을 다시 쪼개지 않는다."""
    text = ("재결요지 요지다.\n"
            "주문 기각한다.\n"
            "이유 앞선 주문 과 같이 판단한다.\n")
    sec = hidrc.split_sections(text)
    assert sec["주문"] == "기각한다."
    assert "앞선 주문 과 같이" in sec["이유"]


def test_hidrc_split_sections_falls_back_to_whole_text():
    assert hidrc.split_sections("표제가 하나도 없는 글")["전문"] == "표제가 하나도 없는 글"


def test_hidrc_rejects_non_pdf():
    with pytest.raises(hidrc.HidrcParseError):
        hidrc.pdf_to_text(b"<html>login required</html>")


# ---------------------------------------------------------------------------
# 국민연금 (재)심사청구 결정사례 (nps.or.kr 화면)
# ---------------------------------------------------------------------------

def test_npsrv_list_parse():
    r = npsrv.parse_list_html((FX / "nps_review_list.html").read_text(encoding="utf-8"))
    assert len(r["items"]) == npsrv.PAGE_SIZE
    # 총건수 표시가 없어 1페이지의 가장 큰 '번호'를 전체 건수로 본다
    assert r["total"] == max(int(i["번호"]) for i in r["items"])
    it = r["items"][0]
    assert it["pstSn"].isdigit()
    assert it["구분"] in ("심사청구", "재심사청구")
    assert it["결정년도"].endswith("년")
    assert it["사례요지"]


def test_npsrv_view_parse_sections():
    v = npsrv.parse_view_html((FX / "nps_review_view.html").read_text(encoding="utf-8"))
    assert v["결정"] in ("기각", "인용", "각하", "일부인용")
    assert v["제목"] and not v["제목"].startswith("<")      # &lt;…&gt; 껍데기를 벗겨야 한다
    assert set(v["본문"]) >= {"처분내용", "청구인주장", "쟁점"}
    assert all(t.strip() for t in v["본문"].values())
    # 표제가 내용에 섞여 들어오면 안 된다
    assert not v["본문"]["처분내용"].startswith("처분내용")


def test_npsrv_view_parse_error_on_layout_change():
    with pytest.raises(npsrv.NpsReviewParseError):
        npsrv.parse_view_html("<html><body>개편됨</body></html>")


def test_npsrv_bad_page_rejected():
    c = npsrv.NpsReviewClient()
    with pytest.raises(ValueError):
        c.list(page=0)
    with pytest.raises(ValueError):
        c.get("숫자아님")


# ---------------------------------------------------------------------------
# 아카이브 등록 — 자료원 코드·별칭이 이어져 있어야 검색 도구가 찾는다
# ---------------------------------------------------------------------------

def test_new_sources_registered_in_archive():
    import archive
    for code in ("prec", "expc", "detc", "hidrc", "npsrv"):
        assert code in archive.SOURCES, f"{code}가 archive.SOURCES에 없다"
    assert archive.resolve_sources("건강보험,국민연금,법원판례") == ["hidrc", "npsrv", "prec"]
    assert archive.resolve_sources("헌재") == ["detc"]
    assert archive.resolve_sources("법제처") == ["expc"]


def test_decc_keywords_cover_social_insurance():
    """4대보험 키워드가 빠지면 건강보험료 부과처분 재결이 통째로 안 들어온다."""
    from ingest_archive import DECC_KEYWORDS
    for kw in ("건강보험", "장기요양", "국민연금", "피부양자", "기준소득월액", "두루누리"):
        assert kw in DECC_KEYWORDS, f"decc 키워드에 {kw}가 없다"


def test_decc_social_insurance_keywords_search_body_not_title():
    """4대보험 사건명은 '보험료부과처분취소청구'처럼 제도명이 없다 — 제목만 훑으면 못 잡는다.

    실제로 제목 목록에만 넣었다가 31건밖에 안 늘었다 (본문 검색으로는 수천 건).
    """
    from ingest_archive import DECC_TITLE_KEYWORDS, DECC_BODY_KEYWORDS
    for kw in ("피부양자", "연금보험료", "기준소득월액", "직장가입자", "두루누리"):
        assert kw in DECC_BODY_KEYWORDS, f"{kw}는 본문 검색 목록에 있어야 한다"
    # 노동 쟁점은 사건명에 드러나므로 제목 검색을 유지한다 (본문 검색은 범위가 너무 넓다)
    for kw in ("해고", "임금", "부당노동행위"):
        assert kw in DECC_TITLE_KEYWORDS and kw not in DECC_BODY_KEYWORDS
    # 너무 일반적인 낱말은 본문 검색에서 뺀다 — 국세·지방세 체납 재결이 쏟아진다
    assert "체납처분" not in DECC_BODY_KEYWORDS


def test_prec_skip_sources_documented():
    """본문이 없거나 comwel과 겹치는 출처는 목록 단계에서 빼야 요청 8만 건을 아낀다."""
    from ingest_archive import PREC_SKIP_SOURCES
    assert PREC_SKIP_SOURCES == {"국세법령정보시스템", "근로복지공단산재판례"}


# ---------------------------------------------------------------------------
# 원천 실호출 (네트워크)
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_hidrc_live_list_and_body():
    c = hidrc.HidrcClient()
    r = c.list(page=1)
    assert r["total"] > 0 and r["items"]
    text = c.get_text(r["items"][0]["첨부일련번호"])
    assert len(text) > 500


@pytest.mark.live
def test_npsrv_live_walk_reaches_total():
    """페이지마다 total을 다시 읽으면 순회가 중간에 끊긴다 — 173건이 다 나와야 한다."""
    c = npsrv.NpsReviewClient()
    first = c.list(1)
    rows = list(c.iter_list())
    assert len(rows) == first["total"]
