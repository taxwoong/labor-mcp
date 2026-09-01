# -*- coding: utf-8 -*-
"""
moel_expc.py — 고용노동부 행정해석(질의회시) 클라이언트
law.go.kr Open API target=moelCgmExpc (중앙부처 1차 법령해석 — 고용노동부)

노무 실무의 핵심 근거인 고용노동부 질의회시를 안건번호("근로기준정책과-3084",
"근기 68207-2140" 등 실무 형식 그대로)와 함께 검색·본문 조회한다.

응답 태그 실측 (2026-08-28, XML 실호출로 확정):
- 목록(lawSearch.do): 루트 <CgmExpc>, 아이템 <cgmExpc id="n">.
  자식: 법령해석일련번호 · 안건명(CDATA) · 안건번호(CDATA) · 질의기관명(대부분 빈값)
  · 해석기관명("고용노동부") · 해석일자(YYYY.MM.DD — 빈값인 건도 있음)
  · 법령해석상세링크 · 데이터기준일시
- 본문(lawService.do?ID=일련번호): 루트 <CgmExpcService>.
  자식: 법령해석일련번호 · 안건명 · 안건번호 · 해석일자(목록과 달리 YYYYMMDD)
  · 해석기관명 · 질의기관명 · 등록일시 · 질의요지(CDATA) · 회답(CDATA)
  · 이유 · 관련법령(둘 다 빈 CDATA인 건이 흔함) · 데이터기준일시
- 없는 ID 조회 시 <Law>일치하는 고용노동부 법령해석이 없습니다…</Law> 반환
  (오류 태그가 아니라 안내문이므로 질의요지·회답 부재로 판별).

파라미터 실측:
- explYd(해석일자 범위, "YYYYMMDD~YYYYMMDD")는 서버측 필터로 동작 —
  '연차' 185건 → explYd=20200101~20241231 지정 시 26건, 반환 일자 전부 범위 내.
- search=1 안건명 검색(안건번호 조각 "근로기준정책과"도 매칭됨, 420건) / search=2 본문 검색.
- sort=ddes(해석일자 내림차순) 동작 확인 — 2026.07.09 해석까지 반환됨. 목록 아이템의
  데이터기준일시 표기(2024.12.16)와 별개로 데이터 자체는 그 후로도 갱신되고 있음.
  sort=dasc는 해석일자 빈값 건이 앞에 몰려 나오므로 실용성 낮음.
"""
import re

from law_go_kr import (
    LawGoKrClient,
    LawGoKrError,
    LawInvalidInput,
    LawNotFound,
    _get,
    _norm_date,
    _parse_items,
    _strip_cdata,
    _strip_tags,
    status_of as _status_of,
)

_DISPLAY_MAX = 100   # law.go.kr이 조용히 절삭하는 상한 (실측)


class MoelExpcClient:
    """고용노동부 행정해석(target=moelCgmExpc) 검색·본문 조회."""

    def __init__(self):
        self._law = LawGoKrClient()  # 법제처 법령해석례(expc) 통합검색용

    # ---------- 목록 검색 ----------
    def search(self, keyword: str, display: int = 10, page: int = 1,
               date_from: str = "", date_to: str = "", search_body: bool = False,
               sort: str = ""):
        """고용노동부 행정해석 검색.

        - keyword: 검색어. 안건번호 조각("근로기준정책과", "근기 68207")도 안건명
          검색(search=1)에서 매칭된다.
        - date_from/date_to: YYYYMMDD 해석일자 범위 (서버측 explYd 필터 — 실측 동작 확인)
        - search_body: True면 본문 검색(search=2), 기본은 안건명 검색(search=1)
        - sort: ""=관련도순(서버 기본), "ddes"=해석일자 내림차순(최신순 — 실측 확인)
        - 반환: {"total": 전체 건수, "items": [{일련번호, 안건명, 안건번호, 해석일자,
          해석기관, (질의기관)}]} — 해석일자는 "YYYY.MM.DD" 표기이며 빈값인 건도 있음.
        """
        if not keyword.strip():
            raise LawInvalidInput("검색어(keyword)가 비어 있습니다")
        # 형식이 어긋난 날짜를 그대로 조립하면 explYd 필터가 통째로 무시된 채 전 기간
        # 결과가 나가고, 사용자는 필터가 걸린 줄 안다 (2026-09-01 실측: 181건 vs 26건)
        date_from = _norm_date(date_from, "date_from")
        date_to = _norm_date(date_to, "date_to")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise LawInvalidInput(f"page는 1 이상의 정수여야 합니다: {page!r}")
        capped = min(int(display), _DISPLAY_MAX)
        params = dict(target="moelCgmExpc", query=keyword, display=capped, page=page,
                      search=2 if search_body else 1)
        if date_from or date_to:
            params["explYd"] = f"{date_from or '19450815'}~{date_to or '20991231'}"
        if sort:
            params["sort"] = sort
        xml = _get("lawSearch.do", **params)
        items = _parse_items(xml, "cgmExpc")  # 루트 <CgmExpc>와 대소문자로 구분됨
        total = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        rows = []
        for it in items:
            row = {
                "일련번호": it.get("법령해석일련번호", ""),
                "안건명": it.get("안건명", ""),
                "안건번호": it.get("안건번호", ""),
                "해석일자": it.get("해석일자", ""),
                "해석기관": it.get("해석기관명", ""),
            }
            if it.get("질의기관명"):
                row["질의기관"] = it["질의기관명"]
            rows.append(row)
        n = int(total.group(1)) if total else len(rows)
        out = {"total": n, "items": rows}
        notices = []
        if capped < int(display):
            notices.append(f"display는 law.go.kr 상한 {_DISPLAY_MAX}건으로 줄여 조회했습니다 "
                           f"(요청 {display}건).")
        if n and not rows:
            pages = max(1, -(-n // capped))
            notices.append(f"요청한 page={page}가 범위를 벗어났습니다 (총 {n}건 / 약 {pages}페이지). "
                           "결과가 없다는 뜻이 아닙니다 — page를 낮춰 다시 조회하세요.")
        elif n == 0:
            notices.append(f"'{keyword}' 검색 결과가 없습니다 (검색은 정상 수행됨). "
                           "검색어를 짧게 바꾸거나 기간 필터를 넓혀 보세요.")
        if notices:
            out["안내"] = " / ".join(notices)
        return out

    # ---------- 본문 조회 ----------
    def get(self, serial: str, max_chars: int = 8000):
        """행정해석 본문(질의요지·회답 전문). serial = 목록의 일련번호.

        반환: {일련번호, 안건명, 안건번호, 해석일자(YYYYMMDD), 해석기관, 질의요지, 회답,
        (질의기관), (등록일시), (이유), (관련법령), (잘림)} — 괄호 항목은 값이 있을 때만.
        본문 없는 일련번호는 LawGoKrError.
        """
        serial = str(serial).strip()
        xml = _get("lawService.do", target="moelCgmExpc", ID=serial)
        d = {"일련번호": serial}
        long_tags = ("질의요지", "회답", "이유")
        for tag in ("안건명", "안건번호", "해석일자", "해석기관명", "질의기관명",
                    "등록일시", "질의요지", "회답", "이유", "관련법령"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if not m:
                continue
            val = _strip_tags(_strip_cdata(m.group(1))).strip()
            if tag not in long_tags:
                # 안건명 CDATA 안에 개행이 섞여 오는 건이 있음(일련번호 17076 실측) — 한 줄로
                val = re.sub(r"\s+", " ", val)
            if not val:
                continue
            key = {"해석기관명": "해석기관", "질의기관명": "질의기관"}.get(tag, tag)
            if tag in long_tags and len(val) > max_chars:
                d[key] = val[:max_chars]
                d.setdefault("잘림", []).append(
                    f"{key}: 전체 {len(val)}자 중 앞 {max_chars}자만 표시 — "
                    f"max_chars를 {len(val)} 이상으로 지정해 다시 조회")
            else:
                d[key] = val if tag in long_tags else val[:2000]
        if "질의요지" not in d and "회답" not in d:
            # 없는 ID면 <Law>일치하는 고용노동부 법령해석이 없습니다…</Law>가 온다 (실측)
            raise LawNotFound(
                f"행정해석 본문 없음 (일련번호 {serial}) — 응답 앞부분: {_strip_tags(xml)[:200].strip()}")
        return d

    # ---------- 통합검색: 고용노동부 행정해석 + 법제처 법령해석례 ----------
    def search_both(self, keyword: str, display: int = 10):
        """moelCgmExpc(고용노동부 질의회시) + expc(법제처 법령해석례) 통합검색.

        반환: {"고용노동부_행정해석": [search()의 items],
              "고용노동부_행정해석_총건수": int,
              "법제처_법령해석례": [{해석례일련번호, 안건명, 안건번호, 회신기관, 회신일자}]}
        한쪽 소스가 실패하면 다른 쪽 결과는 반환하되 "조회상태"로 부분 실패를 알린다.
        **양쪽 다 실패하면 예외를 던진다** — 빈 배열로 반환하면 LLM이 "해당 행정해석이
        없다"고 단정하기 때문이다 (2026-09-01 리뷰: 타임아웃이 0건으로 둔갑).
        """
        out = {"고용노동부_행정해석": [], "법제처_법령해석례": []}
        상태 = {}
        errs = {}
        try:
            moel = self.search(keyword, display=display)
            out["고용노동부_행정해석"] = moel["items"]
            out["고용노동부_행정해석_총건수"] = moel["total"]
            상태["고용노동부"] = "OK" if moel["total"] else "NOT_FOUND"
        except Exception as e:
            상태["고용노동부"] = _status_of(e)
            errs["고용노동부"] = e
            out["고용노동부_행정해석_오류"] = f"{type(e).__name__}: {e}"
        try:
            rows = self._law.search_interpretations(keyword, display=display)
            out["법제처_법령해석례"] = rows
            상태["법제처"] = "OK" if rows else "NOT_FOUND"
        except Exception as e:
            상태["법제처"] = _status_of(e)
            errs["법제처"] = e
            out["법제처_법령해석례_오류"] = f"{type(e).__name__}: {e}"

        if len(errs) == 2:   # 양쪽 다 실패 — 부존재로 오해되지 않도록 예외로 올린다
            raise errs.get("고용노동부", errs["법제처"])

        out["조회상태"] = 상태
        if errs:
            죽은쪽 = ", ".join(errs)
            out["안내"] = (
                f"{죽은쪽} 조회에 실패했습니다 — 그쪽 결과가 비어 있는 것은 "
                "**자료가 없다는 뜻이 아닙니다**. 사용자에게 일부 소스 조회 실패를 알리고, "
                "부존재로 단정하지 마세요.")
        return out
