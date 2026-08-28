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
    _get,
    _parse_items,
    _strip_cdata,
    _strip_tags,
)


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
            raise LawGoKrError("검색어(keyword)가 비어 있습니다")
        params = dict(target="moelCgmExpc", query=keyword, display=display, page=page,
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
        return {
            "total": int(total.group(1)) if total else len(rows),
            "items": rows,
        }

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
            raise LawGoKrError(
                f"행정해석 본문 없음 (일련번호 {serial}) — 응답 앞부분: {_strip_tags(xml)[:200].strip()}")
        return d

    # ---------- 통합검색: 고용노동부 행정해석 + 법제처 법령해석례 ----------
    def search_both(self, keyword: str, display: int = 10):
        """moelCgmExpc(고용노동부 질의회시) + expc(법제처 법령해석례) 통합검색.

        반환: {"고용노동부_행정해석": [search()의 items],
              "고용노동부_행정해석_총건수": int,
              "법제처_법령해석례": [{해석례일련번호, 안건명, 안건번호, 회신기관, 회신일자}]}
        한쪽 소스가 실패해도 다른 쪽 결과는 반환하고, 실패 사유를 "…_오류" 키로 남긴다.
        """
        out = {"고용노동부_행정해석": [], "법제처_법령해석례": []}
        try:
            moel = self.search(keyword, display=display)
            out["고용노동부_행정해석"] = moel["items"]
            out["고용노동부_행정해석_총건수"] = moel["total"]
        except Exception as e:
            out["고용노동부_행정해석_오류"] = str(e)
        try:
            out["법제처_법령해석례"] = self._law.search_interpretations(keyword, display=display)
        except Exception as e:
            out["법제처_법령해석례_오류"] = str(e)
        return out
