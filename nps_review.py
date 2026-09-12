# -*- coding: utf-8 -*-
"""
nps_review.py — 국민연금 (재)심사청구 결정사례 클라이언트 (nps.or.kr 자료실)

국민연금공단 처분에 대한 **이의신청 → 심사청구(국민연금심사위원회) → 재심사청구
(보건복지부 국민연금재심사위원회)** 결정 중 공단이 가려 공개한 사례집이다.
국민연금 분쟁은 특별행정심판이라 중앙행정심판위원회 재결례(law.go.kr target=decc)에
거의 오지 않고, 국민연금재심사위원회는 온라인행정심판 포털에도 재결례를 올리지 않는다
(2026-09-12 실측: simpan.go.kr 국민연금재심사위원회 0건). 사업장가입자 자격·기준소득월액
·납부예외처럼 실무에서 걸리는 쟁점의 사실상 유일한 공개 판단례라 따로 긁는다.
(2026-09-12 실측: 전체 173건, 2020년까지의 결정.)

경로 — 공개 게시판 화면을 그대로 읽는다 (로그인 불필요):
  목록 GET /pnsinfo/ntpsklg/getOHAF0096M0List.do?menuId=MN25000008&pageIndex=N
       한 페이지 10건. 열: 번호 · 구분 · 업무유형 · 세부유형 · 사례요지(링크) · 심의결과 ·
       결정년도 · 조회수. 링크의 pstSn이 글 번호다. 화면에 총건수 표시가 없어 1페이지의
       가장 큰 '번호'를 전체 건수로 본다(게시판이 최신순이라 그 값이 곧 총건수다).
  본문 GET /pnsinfo/ntpsklg/getOHAF0096M1.do?menuId=MN25000008&pstSn=<N>&srclPrdtLclsfCd=1
       div.view-contents-wrap 안에서 <span class="txt-bold txt-large">표제</span> 뒤의
       내용을 표제별로 끊어 읽는다 (처분내용·청구인주장·쟁점·판단).

결정일자는 '결정년도'(연도)뿐이고 월·일이 없다 — 아카이브에는 date_kind='결정연도'로 넣어
확정 일자처럼 보이지 않게 한다 (산재판례의 접수연도와 같은 처리).
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("nps_review")

BASE_URL = "https://www.nps.or.kr"
LIST_URL = f"{BASE_URL}/pnsinfo/ntpsklg/getOHAF0096M0List.do"
VIEW_URL = f"{BASE_URL}/pnsinfo/ntpsklg/getOHAF0096M1.do"
MENU_ID = "MN25000008"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 40
PAGE_SIZE = 10                      # 게시판이 정한 값

# 목록 표의 열 차례 (2026-09-12 실측). 화면이 바뀌면 파싱이 아니라 여기가 먼저 틀어진다.
_COLUMNS = ["번호", "구분", "업무유형", "세부유형", "사례요지", "심의결과", "결정년도", "조회수"]


class NpsReviewError(Exception):
    pass


class NpsReviewUpstreamError(NpsReviewError):
    """네트워크·HTTP 오류 — 자료 부존재와 무관."""


class NpsReviewParseError(NpsReviewError):
    """화면 개편 등으로 해석 실패 — 자료 부존재와 무관."""


def _clean(s: str) -> str:
    s = (s or "").replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_list_html(html: str) -> dict:
    """목록 HTML → {"total": int|None, "items": [{pstSn, 번호, 구분, …}]}"""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.select('a[href*="getOHAF0096M1.do"]'):
        m = re.search(r"pstSn=(\d+)", a.get("href", ""))
        tr = a.find_parent("tr")
        if not m or tr is None:
            continue
        cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        row = {"pstSn": m.group(1), "사례요지": _clean(a.get_text(" "))}
        for name, val in zip(_COLUMNS, cells):
            if name != "사례요지":
                row[name] = val
        items.append(row)
    if not items and "getOHAF0096M" not in html:
        raise NpsReviewParseError("국민연금 결정사례 목록 화면을 인식하지 못했습니다 (구조 변경?)")
    # 총건수 표시가 없다 — 최신순 목록의 가장 큰 '번호'를 전체 건수로 본다
    nums = [int(re.sub(r"\D", "", it.get("번호") or "0") or 0) for it in items]
    total = max(nums) if nums else None
    return {"total": total, "items": items}


def parse_view_html(html: str) -> dict:
    """본문 HTML → {"제목", "결정", "본문": {표제: 내용}}"""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("td.view-title")
    title = _clean(title_el.get_text(" ")).strip("<> ") if title_el else ""
    decision = ""
    info = soup.select_one("div.data-view-info")
    if info:
        m = re.search(r"결정\s*:\s*(\S+)", _clean(info.get_text(" ")))
        if m:
            decision = m.group(1)
    wrap = soup.select_one("div.view-contents-wrap")
    if wrap is None:
        raise NpsReviewParseError("국민연금 결정사례 본문(view-contents-wrap)을 찾지 못했습니다 (구조 변경?)")
    # <span class="txt-bold txt-large">표제</span> 뒤 ~ 다음 표제 앞까지가 그 절의 내용
    sections, current, buf = {}, None, []
    for node in wrap.descendants:
        if getattr(node, "name", None) == "span" and "txt-large" in (node.get("class") or []):
            if current:
                sections[current] = _clean("".join(buf))
            current, buf = _clean(node.get_text(" ")), []
        elif getattr(node, "name", None) == "br":
            buf.append("\n")
        elif isinstance(node, str) and node.strip():
            if node.find_parent("span", class_="txt-large") is None:
                buf.append(node)
    if current:
        sections[current] = _clean("".join(buf))
    if not sections:
        sections = {"전문": _clean(wrap.get_text("\n"))}
    return {"제목": title, "결정": decision, "본문": sections}


class NpsReviewClient:
    def __init__(self, min_interval: float = 0.3):
        self._s = requests.Session()
        self._s.headers["User-Agent"] = USER_AGENT
        self._min_interval = max(0.0, float(min_interval))
        self._last = 0.0

    def _throttle(self):
        el = time.time() - self._last
        if el < self._min_interval:
            time.sleep(self._min_interval - el)
        self._last = time.time()

    def _get(self, url: str, params: dict) -> str:
        self._throttle()
        try:
            r = self._s.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise NpsReviewUpstreamError(
                f"nps.or.kr 접속 실패: {type(e).__name__}: {e} — 자료 부존재와 무관합니다.") from e
        return r.text

    def list(self, page: int = 1, keyword: str = "") -> dict:
        """목록 1페이지(10건). keyword가 있으면 게시판 자체 검색.

        total은 그 페이지의 가장 큰 '번호'라서 **1페이지에서 읽은 값만 전체 건수**다
        (뒤 페이지의 total은 남은 건수에 가깝다 — 순회 종료 판단에 쓰면 중간에 끊긴다).
        """
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")
        params = {"menuId": MENU_ID, "pageIndex": page, "srclPrdtLclsfCd": "1"}
        if keyword and keyword.strip():
            params["searchText"] = keyword.strip()
        res = parse_list_html(self._get(LIST_URL, params))
        res["page"] = page
        return res

    def iter_list(self, keyword: str = "", max_pages: int = 0):
        """목록 전체 순회. 마지막 페이지는 '이전 페이지와 같은 글이 나오면' 판정한다
        (게시판이 범위를 넘는 pageIndex에 마지막 페이지를 그대로 돌려주기 때문)."""
        page, seen, total = 1, set(), None
        while True:
            res = self.list(page=page, keyword=keyword)
            if total is None:                       # 1페이지의 값만 전체 건수다
                total = res["total"]
            fresh = [it for it in res["items"] if it["pstSn"] not in seen]
            if not fresh:
                return
            for it in fresh:
                seen.add(it["pstSn"])
                yield it
            if total and len(seen) >= total:
                return
            if max_pages and page >= max_pages:
                return
            page += 1

    def get(self, pst_sn: str) -> dict:
        sn = re.sub(r"\D", "", str(pst_sn))
        if not sn:
            raise ValueError(f"pstSn은 숫자여야 합니다: {pst_sn!r}")
        d = parse_view_html(self._get(VIEW_URL, {"menuId": MENU_ID, "pstSn": sn,
                                                 "srclPrdtLclsfCd": "1"}))
        d["pstSn"] = sn
        return d
