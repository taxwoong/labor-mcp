# -*- coding: utf-8 -*-
"""
law_committee.py — law.go.kr Open API '위원회 결정문·행정심판례' 클라이언트

노동 실무와 직접 닿는 4개 target을 한 클라이언트로 다룬다 (2026-09-03 실호출로 태그 확정):

| target | 자료 | 전체 건수(빈 검색어) | 본문 핵심 필드 |
|---|---|---|---|
| nlrc   | 노동위원회 결정문 | 44,202 | 자료구분·담당부서·판정사항·판정요지·판정결과 (결정문 전문 '내용'은 대부분 빈값) |
| eiac   | 고용보험심사위원회 결정문 | 118 | 사건의분류·개요·주문·청구취지·이유 전문 |
| iaciac | 산업재해보상보험재심사위원회 결정문 | 934 | 사건대/중/소분류·쟁점·주문·이유 전문 |
| decc   | 행정심판례(중앙행정심판위 등) | 35,170 | 재결청·처분청·주문·청구취지·이유·재결요지 |

공통 실측:
- query를 비우면(또는 '*') 전체 목록이 나온다 → 아카이브 전수 적재 가능.
- sort=ddes(일자 내림차순) 동작. nlrc는 등록일, eiac/iaciac/decc는 의결일자 기준.
- display 최대 100. search=1 제목(사건명), search=2 본문.
- decc만 rslYd(의결일자 범위 YYYYMMDD~YYYYMMDD) 서버측 필터 지원(가이드 문서 기준).
- 목록의 '…상세링크'는 OC가 박힌 DRF 주소라 노출하지 않는다 (law_go_kr.py 규약과 동일).
- 없는 ID 본문 조회는 오류 태그가 아니라 안내문/빈 응답이 온다 → 핵심 필드 부재로 판별.
"""
import re
from typing import Iterator, Optional

from law_go_kr import (
    LawInvalidInput,
    LawNotFound,
    _get,
    _norm_date,
    _parse_items,
    _strip_cdata,
    _strip_tags,
)

_DISPLAY_MAX = 100

TARGETS = {
    "nlrc": {
        "이름": "노동위원회 결정문",
        "item": "nlrc", "id": "결정문일련번호", "root": "NlrcService",
        "list": ("제목", "사건번호", "등록일"), "title": "제목", "date": "등록일",
        "body": ("기관명", "사건번호", "자료구분", "담당부서", "등록일", "제목",
                 "판정사항", "판정요지", "판정결과", "내용"),
        "long": ("판정사항", "판정요지", "내용"),
        "core": ("판정요지", "판정사항", "내용"),
    },
    "eiac": {
        "이름": "고용보험심사위원회 결정문",
        "item": "eiac", "id": "결정문일련번호", "root": "EiacService",
        "list": ("사건명", "사건번호", "의결일자"), "title": "사건명", "date": "의결일자",
        "body": ("사건명", "사건번호", "의결일자", "사건의분류", "의결서종류", "개요",
                 "청구인", "피청구인", "주문", "청구취지", "이유"),
        "long": ("개요", "주문", "청구취지", "이유"),
        "core": ("이유", "주문", "개요"),
    },
    "iaciac": {
        "이름": "산업재해보상보험재심사위원회 결정문",
        "item": "iaciac", "id": "결정문일련번호", "root": "IaciacService",
        "list": ("사건", "사건번호", "의결일자"), "title": "사건", "date": "의결일자",
        "body": ("사건", "사건번호", "의결일자", "사건대분류", "사건중분류", "사건소분류",
                 "쟁점", "청구인", "원처분기관", "주문", "청구취지", "이유", "별지", "문서제공구분"),
        "long": ("쟁점", "주문", "청구취지", "이유", "별지"),
        "core": ("이유", "주문", "쟁점"),
    },
    "decc": {
        "이름": "행정심판례",
        "item": "decc", "id": "행정심판재결례일련번호", "root": "PrecService",
        "list": ("사건명", "사건번호", "처분일자", "의결일자", "처분청", "재결청", "재결구분명"),
        "title": "사건명", "date": "의결일자",
        "body": ("사건명", "사건번호", "처분일자", "의결일자", "처분청", "재결청",
                 "재결례유형명", "주문", "청구취지", "이유", "재결요지"),
        "long": ("주문", "청구취지", "이유", "재결요지"),
        "core": ("이유", "주문", "재결요지"),
    },
}

ALIASES = {
    "노동위원회": "nlrc", "노동위": "nlrc", "중앙노동위원회": "nlrc", "지방노동위원회": "nlrc",
    "고용보험심사위원회": "eiac", "고용보험심사위": "eiac", "고용보험": "eiac",
    "산업재해보상보험재심사위원회": "iaciac", "산재재심사위원회": "iaciac", "산재재심사위": "iaciac",
    "산재재심사": "iaciac", "산재": "iaciac",
    "행정심판": "decc", "행정심판례": "decc", "행정심판례집": "decc", "중앙행정심판위원회": "decc",
}


def resolve_target(source: str) -> str:
    s = (source or "").strip()
    code = ALIASES.get(s, s)
    if code not in TARGETS:
        raise LawInvalidInput(
            f"알 수 없는 자료원 {source!r} — 사용 가능: {', '.join(TARGETS)} "
            f"(한글: {', '.join(k for k in ALIASES if len(k) > 2)})")
    return code


_HIDDEN_RE = re.compile(r"<span[^>]*display\s*:\s*none[^>]*>.*?</span>", re.S | re.I)


def _clean(val: str, one_line: bool) -> str:
    # 주문 필드 앞에 화면용 숨김 span("주       문")이 붙어 온다 (eiac 실측) — 태그째 제거
    v = _HIDDEN_RE.sub("", val or "")
    v = _strip_tags(_strip_cdata(v)).strip()
    v = v.replace("\r", "")
    if one_line:
        v = re.sub(r"\s+", " ", v)
    else:
        v = re.sub(r"[ \t]+\n", "\n", v)
        v = re.sub(r"\n{3,}", "\n\n", v)
    return v


class CommitteeClient:
    """위원회 결정문·행정심판례 목록 검색과 본문 조회."""

    # ---------- 목록 ----------
    def search(self, source: str, keyword: str = "", display: int = 10, page: int = 1,
               search_body: bool = False, sort: str = "", date_from: str = "",
               date_to: str = "") -> dict:
        """목록 검색. keyword를 비우면 전체(최신순 권장).

        - sort: "" 관련도(서버 기본) | "ddes" 일자 내림차순 | "dasc" 오름차순
        - date_from/date_to: YYYYMMDD — decc는 서버측 rslYd 필터, 그 외는 클라이언트 필터
        - 반환 {"자료원", "total", "items": [{일련번호, 제목, 사건번호, 일자, ...}]}
        """
        code = resolve_target(source)
        spec = TARGETS[code]
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise LawInvalidInput(f"page는 1 이상의 정수여야 합니다: {page!r}")
        date_from = _norm_date(date_from, "date_from")
        date_to = _norm_date(date_to, "date_to")
        params = dict(target=code, query=keyword.strip() if keyword else "",
                      display=min(max(int(display), 1), _DISPLAY_MAX), page=page,
                      search=2 if search_body else 1)
        if sort:
            params["sort"] = sort
        if code == "decc" and (date_from or date_to):
            params["rslYd"] = f"{date_from or '19450815'}~{date_to or '20991231'}"
        xml = _get("lawSearch.do", **params)
        m = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        total = int(m.group(1)) if m else 0
        items = []
        for it in _parse_items(xml, spec["item"]):
            serial = it.get(spec["id"], "")
            if not serial:
                continue
            row = {"일련번호": serial, "제목": _clean(it.get(spec["title"], ""), True)}
            for tag in spec["list"]:
                if tag == spec["title"]:
                    continue
                v = _clean(it.get(tag, ""), True)
                if v:
                    row[tag] = v
            row["일자"] = row.get(spec["date"], "")
            items.append(row)
        if code != "decc" and (date_from or date_to):
            def _key(d):
                return re.sub(r"\D", "", d or "")[:8]
            items = [r for r in items
                     if (not date_from or _key(r["일자"]) >= date_from)
                     and (not date_to or _key(r["일자"]) <= date_to)]
        return {"자료원": spec["이름"], "target": code, "total": total, "items": items}

    def iter_list(self, source: str, keyword: str = "", sort: str = "ddes",
                  display: int = _DISPLAY_MAX, search_body: bool = False,
                  max_pages: int = 100000, on_page=None) -> Iterator[dict]:
        """목록 전 페이지 순회 (아카이브 적재용). on_page(page, total, n_items) 콜백 선택."""
        page = 1
        while page <= max_pages:
            res = self.search(source, keyword, display=display, page=page, sort=sort,
                              search_body=search_body)
            items = res["items"]
            if on_page:
                on_page(page, res["total"], len(items))
            if not items:
                return
            yield from items
            if page * display >= res["total"]:
                return
            page += 1

    # ---------- 본문 ----------
    def get(self, source: str, serial: str, max_chars: int = 8000) -> dict:
        """본문 조회. 핵심 필드(판정요지/이유 등)가 하나도 없으면 LawNotFound."""
        code = resolve_target(source)
        spec = TARGETS[code]
        serial = str(serial).strip()
        if not serial.isdigit():
            raise LawInvalidInput(f"일련번호는 숫자여야 합니다: {serial!r} (목록의 '일련번호')")
        xml = _get("lawService.do", target=code, ID=serial)
        d = {"자료원": spec["이름"], "target": code, "일련번호": serial}
        for tag in spec["body"]:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if not m:
                continue
            val = _clean(m.group(1), one_line=tag not in spec["long"])
            if not val:
                continue
            if tag in spec["long"] and len(val) > max_chars:
                d[tag] = val[:max_chars]
                d.setdefault("잘림", []).append(
                    f"{tag}: 전체 {len(val)}자 중 앞 {max_chars}자만 표시 — "
                    f"max_chars를 {len(val)} 이상으로 지정해 다시 조회")
            else:
                d[tag] = val
        if not any(k in d for k in spec["core"]):
            raise LawNotFound(
                f"{spec['이름']} 본문 없음 (일련번호 {serial}) — 응답 앞부분: "
                f"{_strip_tags(xml)[:200].strip()}")
        d["제목"] = d.get(spec["title"], "")
        return d
