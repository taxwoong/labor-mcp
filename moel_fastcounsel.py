# -*- coding: utf-8 -*-
"""
moel_fastcounsel.py — 고용노동부 빠른인터넷상담 게시판 클라이언트 (moel.go.kr)

공식 API가 없어 공개 게시판 화면을 그대로 읽는다 (2026-09-03 실측: 전체 104,053건, 로그인 불필요).

- 목록: GET /minwon/fastcounsel/fastcounselList.do
    pageIndex(페이지), pageUnit(10/20/30/40/50 — 50까지 동작 확인),
    searchField(1 질문 / 2 답변 / 3 질문+답변), searchText(검색어)
  행: 번호 · 질문(제목, a[title]에 전문) · 등록일 · 답변여부. 링크는 inetDcssMngId=<21자리>.
- 본문: GET /minwon/fastcounsel/fastcounselView.do?inetDcssMngId=…
    div.board_view_wrap 안의 <dl><dt>질의</dt><dd>…</dd></dl><dl><dt>답변</dt><dd>…</dd></dl>.
    본문 화면에는 날짜가 없다 — 날짜는 목록에서 가져온다.
- 목록 한 페이지(50건)는 약 2초, 본문은 0.05초 안팎.
"""
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("fastcounsel")

BASE_URL = "https://www.moel.go.kr"
LIST_URL = f"{BASE_URL}/minwon/fastcounsel/fastcounselList.do"
VIEW_URL = f"{BASE_URL}/minwon/fastcounsel/fastcounselView.do"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30
FIELD_CODES = {"질문": "1", "답변": "2", "질문+답변": "3", "1": "1", "2": "2", "3": "3"}


class FastCounselError(Exception):
    pass


class FastCounselUpstreamError(FastCounselError):
    """네트워크·HTTP 오류 — 자료 부존재와 무관."""


class FastCounselParseError(FastCounselError):
    """화면 개편 등으로 해석 실패 — 자료 부존재와 무관."""


def _clean(s: str) -> str:
    s = (s or "").replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_list_html(html: str) -> dict:
    """목록 HTML → {"total": int|None, "items": [{id, 번호, 제목, 등록일, 답변여부}]}"""
    soup = BeautifulSoup(html, "html.parser")
    total = None
    m = re.search(r"전체\s*(?:<[^>]*>\s*)*([\d,]+)", html)
    if m:
        total = int(m.group(1).replace(",", ""))
    items = []
    for a in soup.select('a[href*="inetDcssMngId="]'):
        mid = re.search(r"inetDcssMngId=(\d+)", a.get("href", ""))
        if not mid:
            continue
        tr = a.find_parent("tr")
        if tr is None:
            continue
        title = _clean(a.get("title") or a.get_text(" "))
        row = {"id": mid.group(1), "제목": title}
        for td in tr.find_all("td"):
            label = td.get("aria-label", "")
            if label == "번호":
                row["번호"] = _clean(td.get_text())
            elif label == "등록일":
                row["등록일"] = _clean(td.get_text())
            elif label == "답변여부":
                row["답변여부"] = _clean(td.get_text(" "))
        items.append(row)
    if not items and total is None and "fastcounsel" not in html:
        raise FastCounselParseError("빠른인터넷상담 목록 화면을 인식하지 못했습니다 (구조 변경?)")
    return {"total": total, "items": items}


def parse_view_html(html: str) -> dict:
    """본문 HTML → {"질의": str, "답변": str}. 둘 다 없으면 ParseError."""
    soup = BeautifulSoup(html, "html.parser")
    wrap = soup.select_one("div.board_view_wrap") or soup
    out = {}
    for dl in wrap.find_all("dl"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if not dt or not dd:
            continue
        key = _clean(dt.get_text())
        if key in ("질의", "답변"):
            out[key] = _clean(dd.get_text("\n"))
    if not out:
        raise FastCounselParseError("빠른인터넷상담 본문(질의/답변 블록)을 찾지 못했습니다 (구조 변경?)")
    return out


class FastCounselClient:
    def __init__(self, min_interval: float = 0.3):
        self._s = requests.Session()
        self._s.headers["User-Agent"] = USER_AGENT
        self._min_interval = min_interval
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
            raise FastCounselUpstreamError(
                f"moel.go.kr 접속 실패: {type(e).__name__}: {e} — 자료 부존재와 무관합니다.") from e
        return r.text

    def list(self, page: int = 1, unit: int = 50, keyword: str = "", field: str = "3") -> dict:
        """목록 1페이지. keyword가 있으면 게시판 자체 검색(필드: 질문/답변/질문+답변)."""
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")
        unit = unit if unit in (10, 20, 30, 40, 50) else 50
        params = {"pageIndex": page, "pageUnit": unit}
        if keyword and keyword.strip():
            code = FIELD_CODES.get(str(field).strip(), "3")
            params.update({"searchField": code, "searchText": keyword.strip()})
        res = parse_list_html(self._get(LIST_URL, params))
        res["page"] = page
        return res

    def get(self, post_id: str) -> dict:
        pid = re.sub(r"\D", "", str(post_id))
        if not pid:
            raise ValueError(f"post_id는 숫자여야 합니다: {post_id!r}")
        d = parse_view_html(self._get(VIEW_URL, {"inetDcssMngId": pid}))
        d["id"] = pid
        return d
