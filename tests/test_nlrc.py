# -*- coding: utf-8 -*-
"""
test_nlrc.py — nlrc.py 스크래퍼 테스트

- 파서 단위 테스트: research/nlrc_list.html (2026-08-28 실응답 픽스처) 기반, 네트워크 불필요
- 실호출 테스트: @pytest.mark.live — `pytest -m live`로만 실행
  (기본 실행에서 제외하려면 `pytest -m "not live"`)
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import nlrc  # noqa: E402
from nlrc import (  # noqa: E402
    CATEGORIES,
    CATEGORY_NAME_BY_CODE,
    NlrcClient,
    NlrcParseError,
    _detail_type,
    _normalize_date,
    _resolve_categories,
    _resolve_committee,
    parse_detail_html,
    parse_list_html,
)

FIXTURE = PROJECT_ROOT / "research" / "nlrc_list.html"


@pytest.fixture(scope="module")
def parsed_fixture():
    return parse_list_html(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 파서 단위 테스트 (픽스처 — 네트워크 불필요)
# ---------------------------------------------------------------------------

def test_fixture_has_six_categories(parsed_fixture):
    names = [c["구분"] for c in parsed_fixture["categories"]]
    # 검색폼 체크박스는 '교섭창구 단일화 절차'지만 목록 응답 헤더는
    # '교섭대표 단일화 절차'로 내려온다 (사이트 자체 표기 불일치 — 실측)
    assert names == [
        "부당해고·부당노동행위", "차별시정", "교섭대표 단일화 절차",
        "교섭단위 결정", "공정대표 의무위반", "기타판정",
    ]


def test_fixture_totals(parsed_fixture):
    totals = {c["구분"]: c["total"] for c in parsed_fixture["categories"]}
    # 픽스처 채집 시점(2026-08-28)의 실측 건수
    assert totals["부당해고·부당노동행위"] == 44883
    assert totals["차별시정"] == 42
    # 통합(복수 구분) 응답에는 페이지네이션이 없다
    assert parsed_fixture["total_pages"] is None


def test_fixture_first_item_fields(parsed_fixture):
    item = parsed_fixture["categories"][0]["items"][0]
    assert item["구분"] == "부당해고·부당노동행위"
    assert item["위원회"] == "전남지방노동위원회"
    assert item["사건번호"] == "2026부해618"
    assert item["판정일"] == "2026-08-19"
    assert item["결과"] == "기각"
    # 하이라이트 <b> 태그가 사건명 중간에서 벌어지지 않아야 함
    assert item["사건명"] == "○ ○ ○ 부당해고 구제신청"
    assert item["판정요지"].startswith("이 사건 인사발령은 조직 내 갈등을 해소")
    assert item["detail_keys"]["k1"] == "05"
    assert item["detail_keys"]["k2"] == "2606220856260010840"
    assert item["detail_keys"]["k3"] == "JR01"
    assert item["detail_keys"]["table_nm"] == "TBMREA1000F"


def test_fixture_summary_highlight_stripped(parsed_fixture):
    """판정요지의 이스케이프된 하이라이트('&lt;b&gt;해고&lt;/b&gt;')가 제거되는지."""
    items = parsed_fixture["categories"][0]["items"]
    target = next(it for it in items if it["사건번호"] == "2026부해359")
    assert "<b>" not in target["판정요지"]
    assert "해고는 존재하지 아니하며" in target["판정요지"]


def test_fixture_all_items_have_required_fields(parsed_fixture):
    count = 0
    for cat in parsed_fixture["categories"]:
        for it in cat["items"]:
            count += 1
            assert it["위원회"]
            assert it["사건번호"]
            assert it["판정일"]
            assert it["판정요지"]
            assert it["detail_keys"]["k2"]
    assert count == 26  # 픽스처 실측 항목 수


def test_category_mapping():
    assert _resolve_categories(None) == ["BH", "DR", "RP", "SP", "GJ", "ET"]
    assert _resolve_categories("부당해고") == ["BH"]
    assert _resolve_categories(["차별시정", "기타"]) == ["DR", "ET"]
    assert _resolve_categories(["BH"]) == ["BH"]  # 코드 직접 입력 허용
    with pytest.raises(ValueError):
        _resolve_categories(["없는구분"])
    # 사람이 읽는 키와 코드가 1:1인지
    assert set(CATEGORIES.values()) == set(CATEGORY_NAME_BY_CODE.keys())


def test_committee_mapping():
    assert _resolve_committee("") == "all"
    assert _resolve_committee("중앙") == "00"
    assert _resolve_committee("서울지방노동위원회") == "01"
    assert _resolve_committee("13") == "13"
    with pytest.raises(ValueError):
        _resolve_committee("화성")


def test_normalize_date():
    assert _normalize_date("") == ""
    assert _normalize_date("20250101") == "2025-01-01"
    assert _normalize_date("2025-01-01") == "2025-01-01"
    assert _normalize_date("2025.01.01") == "2025-01-01"
    with pytest.raises(ValueError):
        _normalize_date("2025-1-1")  # 자릿수 부족은 명시적 오류


def test_detail_type_mapping():
    # 판정요지 목록의 실측 even_gubn 계열
    assert _detail_type("JR01", "20") == ("brjuPoin", "06")
    assert _detail_type("NR30", "20") == ("brjuPoin", "05")
    assert _detail_type("NS31", "10") == ("brjuPoin", "05")
    assert _detail_type("DS", "10") == ("brjuPoin", "06")
    # 조정(ME) — 취하는 결정서 없음
    assert _detail_type("ME", "10") == ("mediPlan", "01")
    assert _detail_type("ME", "30") == ("defnShet", "02")
    from nlrc import NlrcError
    with pytest.raises(NlrcError):
        _detail_type("ME", "11")


def test_parse_detail_empty_table_is_not_found():
    html = """<div class="BD_table"><table><tbody>
        <tr><th>판정요지</th></tr>
        <tr><td>사건 :</td></tr>
        <tr><th>판정사항</th></tr>
        <tr><th>판정요지</th></tr>
    </tbody></table></div>"""
    result = parse_detail_html(html)
    assert result["found"] is False


def test_parse_detail_sections():
    html = """<div class="BD_table"><table><tbody>
        <tr><th>전남지방노동위원회\n판정요지</th></tr>
        <tr><td>사건 : 2026부해618</td></tr>
        <tr><th>판정사항</th></tr>
        <tr><td><pre>판정사항 본문</pre></td></tr>
        <tr><th>판정요지</th></tr>
        <tr><td><pre>판정요지 본문</pre></td></tr>
    </tbody></table></div>"""
    result = parse_detail_html(html)
    assert result["found"] is True
    assert result["위원회"] == "전남지방노동위원회"
    assert result["사건번호"] == "2026부해618"
    assert result["sections"] == {"판정사항": "판정사항 본문", "판정요지": "판정요지 본문"}


def test_search_rejects_empty_keyword():
    client = NlrcClient()
    with pytest.raises(ValueError):
        client.search("")
    with pytest.raises(ValueError):
        client.search("!@#")  # 특수문자만 있으면 제거 후 빈 검색어


# ---------------------------------------------------------------------------
# 실호출 테스트 — pytest -m live
# ---------------------------------------------------------------------------

live = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    return NlrcClient()


@live
def test_live_search_single_category(client):
    res = client.search("해고", categories=["부당해고"], page=1)
    assert res["total"] and res["total"] > 40000  # 2026-08-28 기준 44,886건
    assert len(res["items"]) == 10  # 단일 구분 = 페이지당 10건
    assert res["total_pages"] and res["total_pages"] > 4000
    # 판정요지 인라인 파싱 — 2건 이상 샘플 확인
    with_summary = [it for it in res["items"] if len(it["판정요지"]) > 30]
    assert len(with_summary) >= 2
    for it in res["items"]:
        assert it["구분"] == "부당해고·부당노동행위"
        assert it["사건번호"]
        assert it["detail_keys"]["k2"]


@live
def test_live_pagination_differs(client):
    p1 = client.search("해고", categories=["부당해고"], page=1)
    p2 = client.search("해고", categories=["부당해고"], page=2)
    assert {it["사건번호"] for it in p1["items"]} != {it["사건번호"] for it in p2["items"]}


@live
def test_live_date_filter_serverside(client):
    res = client.search("해고", categories=["부당해고"],
                        date_from="20250101", date_to="20250331")
    assert res["items"]
    # 전체 44,886건 대비 대폭 줄어야 서버측 필터가 동작한 것 (실측 1,405건)
    assert res["total"] < 5000
    for it in res["items"]:
        assert "2025-01-01" <= it["판정일"] <= "2025-03-31"


@live
def test_live_committee_filter_serverside(client):
    res = client.search("해고", categories=["부당해고"], committee="중앙")
    assert res["items"]
    assert res["total"] < 44000  # 전체보다 확실히 적어야 함 (실측 11,950건)
    for it in res["items"]:
        assert it["위원회"] == "중앙노동위원회"


@live
def test_live_category_filter_serverside(client):
    res = client.search("교섭", categories=["교섭창구단일화"])
    assert res["items"]
    for it in res["items"]:
        assert it["구분"] == "교섭대표 단일화 절차"  # 목록 응답 헤더 표기 (실측)


@live
def test_live_multi_category_preview(client):
    res = client.search("해고")  # 전체 6개 구분
    assert "notice" in res
    names = {it["구분"] for it in res["items"]}
    assert "부당해고·부당노동행위" in names
    assert len(names) >= 2


@live
def test_live_get_detail(client):
    res = client.search("해고", categories=["부당해고"], page=1)
    detail = client.get_detail(res["items"][0]["detail_keys"])
    assert detail["found"] is True
    assert detail["위원회"]
    assert detail["사건번호"]
    # 판정사항·판정요지(또는 결정사항·결정요지) 섹션 존재
    assert any("요지" in k for k in detail["sections"])
    assert all(len(v) > 10 for v in detail["sections"].values())


@live
def test_live_get_detail_bad_keys_not_found(client):
    bad = {"k1": "99", "k2": "NOPE", "k3": "JR01", "k4": "N",
           "k5": "20", "k6": "N", "k7": "N", "k8": ""}
    detail = client.get_detail(bad, use_cache=False)
    assert detail["found"] is False


# ---------------------------------------------------------------------------
# 파서 생존 판정 (2026-09-01 추가)
#
# 스크래핑 서버의 최대 리스크는 "사이트 개편으로 소리 없이 깨져서 0건처럼 보이는 것"이다.
# 아래 세 케이스는 실제 픽스처를 훼손해 개편을 흉내 내고, search()가 이를
# NlrcParseError로 구분해 내는지 검증한다. 개편을 "검색 결과 없음"으로 반환하면 실패.
# ---------------------------------------------------------------------------
import re as _re


def _client_returning(html):
    """_post를 고정 HTML로 대체한 클라이언트 — 네트워크 없이 search() 전체 경로를 탄다."""
    c = nlrc.NlrcClient.__new__(nlrc.NlrcClient)
    c._cache = {}
    c._cache_ttl = 0
    c._post = lambda url, data, validate: html
    return c


@pytest.fixture(scope="module")
def fixture_html():
    return FIXTURE.read_text(encoding="utf-8")


def test_정상_픽스처는_통과한다(fixture_html):
    r = _client_returning(fixture_html).search("해고", use_cache=False)
    assert r["items"], "정상 픽스처인데 항목을 못 읽었다"
    assert r["total"]


def test_결과영역_마크업_교체는_PARSE_ERROR(fixture_html):
    """결과 컨테이너가 통째로 바뀐 경우 — 예전에는 '검색 결과가 없습니다'로 나갔다."""
    broken = fixture_html.replace('class="C_body"', 'class="Result_body_v2"')
    with pytest.raises(nlrc.NlrcParseError):
        _client_returning(broken).search("해고", use_cache=False)


def test_항목_클래스명_변경은_PARSE_ERROR(fixture_html):
    """총건수는 읽히는데 목록만 못 읽는 경우 — total>0 & items=0."""
    broken = fixture_html.replace('<dl class="C_Cts"', '<dl class="C_Cts_v2"')
    with pytest.raises(nlrc.NlrcParseError) as e:
        _client_returning(broken).search("해고", use_cache=False)
    assert "부존재" in str(e.value)


def test_필드_셀렉터_변경은_PARSE_ERROR(fixture_html):
    """items 개수는 정상인데 내용만 비는 부분 붕괴 — 구조 카나리만으로는 못 잡는다."""
    broken = _re.sub(r'<em class="date"', '<em class="dt"', fixture_html)
    with pytest.raises(nlrc.NlrcParseError) as e:
        _client_returning(broken).search("해고", use_cache=False)
    assert "판정일" in str(e.value)


def test_page_음수는_거부한다(fixture_html):
    with pytest.raises(ValueError):
        _client_returning(fixture_html).search("해고", page=0, use_cache=False)
