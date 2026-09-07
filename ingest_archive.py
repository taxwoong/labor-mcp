# -*- coding: utf-8 -*-
"""
ingest_archive.py — 노무 사례 아카이브 적재 CLI (archive.py의 SQLite DB에 원천 자료를 쌓는다)

    python ingest_archive.py all                 # 전 자료원 증분 적재 (매월 자동 실행용)
    python ingest_archive.py nlrc moel            # 자료원 지정
    python ingest_archive.py nlrc --limit 200     # 시험 적재 (새 문서 200건에서 중단)
    python ingest_archive.py all --full           # 기존 문서도 다시 받아 갱신
    python ingest_archive.py --stats              # 적재 현황

자료원(코드): nlrc 노동위원회 결정문 · moel 고용노동부 행정해석 · eiac 고용보험심사위 ·
  iaciac 산재재심사위 · decc 행정심판례(노동 키워드) · admrul 고용노동부 행정규칙 ·
  counsel 빠른인터넷상담 · qnabook 근로기준법 질의회시집 PDF · comwel 근로복지공단 산재판례

증분 규칙: law.go.kr 계열은 목록을 최신순으로 **끝까지** 훑되(목록 호출은 100건 단위라 싸다)
본문은 DB에 없는 일련번호만 받는다. 빠른인터넷상담은 목록 한 페이지가 2초라 "새 글이 하나도
없는 페이지"를 만나면 멈춘다. --full이면 전부 다시 받는다.

재확인 대기(pending_recheck): 지금은 담을 수 없는 글 — 빠른인터넷상담의 '미완료'(답변 전) 글과
본문 조회에 실패한 글 — 을 적어 두고 **다음 갱신 때 먼저 다시 본다**. 이게 없으면, 나중에
답변이 달려도 그 글은 목록 깊숙이 밀려나 증분 스캔에 다시 걸리지 않아 영구 누락된다
(2026-09-07 실측: 최신 200건 중 19건이 미완료 상태).

원천 부하: 요청 간격(--interval, 기본 0.3초)을 지키고, 인증·한도 오류(LawAuthError)가 나면
그 자료원은 즉시 중단한다. 일시 오류는 3회 재시도 후 건너뛴다.
필요 환경변수: LAW_API_OC (law.go.kr 계열), DATA_GO_KR_KEY (comwel). run_server.bat과 같은
local_env.bat을 refresh_archive.bat이 먼저 읽는다.
"""
import argparse
import json
import logging
import re
import sys
import time
from datetime import date as _date
from pathlib import Path

import requests

import archive
from law_go_kr import (
    LawAuthError,
    LawGoKrError,
    LawNotFound,
    LawUpstreamError,
    _get,
    _parse_items,
    _strip_cdata,
    _strip_tags,
)
from law_committee import CommitteeClient, TARGETS
from moel_expc import MoelExpcClient

LOG = logging.getLogger("ingest")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MOEL_ORG = "1492000"          # 고용노동부 소관부처 코드 (law.go.kr admrul org=)
DECC_KEYWORDS = ["근로", "임금", "해고", "산재", "산업재해", "요양", "장해", "유족", "실업급여",
                 "고용보험", "고용노동", "노동", "사업주", "근로자", "퇴직", "휴업", "직업훈련",
                 "육아휴직", "출산", "체불", "최저임금", "부당노동행위", "노동조합", "고용허가",
                 "외국인근로자", "산업안전", "중대재해", "직장 내 괴롭힘", "성희롱"]
_TRANSIENT = (LawUpstreamError, requests.exceptions.RequestException)


class Throttle:
    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
        self._last = 0.0

    def wait(self):
        el = time.time() - self._last
        if el < self.interval:
            time.sleep(self.interval - el)
        self._last = time.time()


class Runner:
    def __init__(self, conn, interval: float = 0.3, full: bool = False, limit: int = 0,
                 max_pages: int = 0, pdf_path: str = ""):
        self.conn = conn
        self.throttle = Throttle(interval)
        self.full = full
        self.limit = int(limit or 0)
        self.max_pages = int(max_pages or 0)
        self.pdf_path = pdf_path
        self.committee = CommitteeClient()
        self.moel = MoelExpcClient()

    # ---------- 공통 ----------
    def _call(self, fn, what: str, tries: int = 3):
        last = None
        for i in range(tries):
            self.throttle.wait()
            try:
                return fn()
            except LawAuthError:
                raise
            except _TRANSIENT as e:                      # 일시 오류 — 재시도
                last = e
                LOG.warning("%s 일시 오류(%d/%d): %s", what, i + 1, tries, str(e)[:160])
                time.sleep(3 * (i + 1))
        raise last

    def _batch(self, source: str, rows, fetch_body, map_doc):
        """rows(목록 항목) 중 DB에 없는 것만 본문을 받아 저장. 반환 (추가, 실패)."""
        known = set() if self.full else archive.known_ids(self.conn, source)
        added = failed = skipped = 0
        consecutive = 0
        t0 = time.time()
        for row in rows:
            doc_id = str(row["_id"])
            if doc_id in known:
                skipped += 1
                continue
            try:
                body = self._call(lambda: fetch_body(row), f"{source} 본문 {doc_id}")
                consecutive = 0
            except LawNotFound as e:
                LOG.info("%s %s 본문 없음 — 건너뜀 (%s)", source, doc_id, str(e)[:80])
                failed += 1
                continue
            except _TRANSIENT as e:
                failed += 1
                consecutive += 1
                LOG.error("%s %s 본문 실패 — 건너뜀: %s", source, doc_id, str(e)[:120])
                if consecutive >= 20:
                    raise LawUpstreamError(f"{source}: 연속 {consecutive}건 실패 — 원천 장애로 보고 중단")
                continue
            doc = map_doc(row, body)
            archive.upsert(self.conn, source, doc_id, **doc)
            added += 1
            known.add(doc_id)
            if added % 50 == 0:
                self.conn.commit()
            if added % 200 == 0:
                LOG.info("%s 진행: 추가 %d · 실패 %d · 기존 %d · %.0f초", source, added, failed, skipped, time.time() - t0)
            if self.limit and added >= self.limit:
                LOG.info("%s --limit %d 도달, 중단", source, self.limit)
                break
        self.conn.commit()
        return added, failed

    def _walk_law_list(self, target: str, id_tag: str, item_tag: str, **params):
        """law.go.kr 목록을 최신순으로 끝까지 순회. 항목 dict에 '_id'를 붙여 yield."""
        page, seen = 1, set()
        while True:
            p = dict(target=target, display=100, page=page, sort="ddes")
            p.update(params)
            xml = self._call(lambda: _get("lawSearch.do", **p), f"{target} 목록 p{page}")
            m = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
            total = int(m.group(1)) if m else 0
            items = _parse_items(xml, item_tag)
            if page == 1:
                LOG.info("%s 목록 전체 %d건 (%d페이지)", target, total, (total + 99) // 100)
            if not items:
                return
            for it in items:
                sid = it.get(id_tag, "")
                if sid and sid not in seen:
                    seen.add(sid)
                    it["_id"] = sid
                    yield it
            if page * 100 >= total or (self.max_pages and page >= self.max_pages):
                return
            page += 1

    # ---------- law.go.kr 위원회 결정문 (nlrc/eiac/iaciac) ----------
    def ingest_committee(self, code: str) -> tuple:
        spec = TARGETS[code]
        rows = self._walk_law_list(code, spec["id"], spec["item"])

        def fetch(row):
            return self.committee.get(code, row["_id"], max_chars=10_000_000)

        def mapper(row, b):
            if code == "nlrc":
                body = "\n\n".join(x for x in (b.get("판정요지", ""), b.get("내용", "")) if x)
                return dict(title=b.get("제목", ""), doc_no=b.get("사건번호", ""), doc_date=b.get("등록일", ""),
                            category=b.get("자료구분", ""), org=b.get("담당부서", ""),
                            summary=b.get("판정사항", ""), body=body,
                            extra={k: b[k] for k in ("판정결과", "기관명") if b.get(k)})
            if code == "eiac":
                body = _join(("주문", b.get("주문")), ("청구취지", b.get("청구취지")), ("이유", b.get("이유")))
                return dict(title=b.get("사건명", ""), doc_no=b.get("사건번호", ""), doc_date=b.get("의결일자", ""),
                            category=b.get("사건의분류", ""), org="고용보험심사위원회",
                            summary=b.get("개요", ""), body=body,
                            extra={k: b[k] for k in ("의결서종류", "청구인", "피청구인") if b.get(k)})
            if code == "iaciac":
                cat = " > ".join(x for x in (b.get("사건대분류"), b.get("사건중분류"), b.get("사건소분류")) if x)
                body = _join(("주문", b.get("주문")), ("청구취지", b.get("청구취지")), ("이유", b.get("이유")),
                             ("별지", b.get("별지")))
                return dict(title=b.get("사건", ""), doc_no=b.get("사건번호", ""), doc_date=b.get("의결일자", ""),
                            category=cat, org=b.get("원처분기관", ""), summary=b.get("쟁점", ""), body=body,
                            extra={k: b[k] for k in ("청구인", "문서제공구분") if b.get(k)})
            raise ValueError(code)

        return self._batch(code, rows, fetch, mapper)

    # ---------- law.go.kr 행정심판례 — 노동 키워드 합집합 ----------
    def ingest_decc(self) -> tuple:
        spec = TARGETS["decc"]

        def rows():
            seen = set()
            for kw in DECC_KEYWORDS:
                n = 0
                for it in self._walk_law_list("decc", spec["id"], spec["item"], query=kw, search=1):
                    if it["_id"] in seen:
                        continue
                    seen.add(it["_id"])
                    n += 1
                    yield it
                LOG.info("decc 키워드 %r: 신규 후보 %d건 (누적 %d)", kw, n, len(seen))

        def fetch(row):
            return self.committee.get("decc", row["_id"], max_chars=10_000_000)

        def mapper(row, b):
            body = _join(("주문", b.get("주문")), ("청구취지", b.get("청구취지")), ("이유", b.get("이유")))
            return dict(title=b.get("사건명", ""), doc_no=b.get("사건번호", ""), doc_date=b.get("의결일자", ""),
                        category=b.get("재결례유형명", ""), org=b.get("재결청", ""),
                        summary=b.get("재결요지", ""), body=body,
                        extra={k: b[k] for k in ("처분청", "처분일자") if b.get(k)})

        return self._batch("decc", rows(), fetch, mapper)

    # ---------- law.go.kr 고용노동부 행정해석 ----------
    def ingest_moel(self) -> tuple:
        rows = self._walk_law_list("moelCgmExpc", "법령해석일련번호", "cgmExpc")

        def fetch(row):
            return self.moel.get(row["_id"], max_chars=10_000_000)

        def mapper(row, b):
            body = _join(("질의요지", b.get("질의요지")), ("회답", b.get("회답")), ("이유", b.get("이유")),
                         ("관련법령", b.get("관련법령")))
            return dict(title=b.get("안건명", ""), doc_no=b.get("안건번호", ""), doc_date=b.get("해석일자", ""),
                        org=b.get("해석기관", "고용노동부"), summary=(b.get("질의요지") or "")[:600], body=body,
                        extra={k: b[k] for k in ("질의기관", "등록일시") if b.get(k)})

        return self._batch("moel", rows, fetch, mapper)

    # ---------- law.go.kr 고용노동부 행정규칙 ----------
    def ingest_admrul(self) -> tuple:
        rows = self._walk_law_list("admrul", "행정규칙일련번호", "admrul", org=MOEL_ORG, nw=1)

        def fetch(row):
            xml = _get("lawService.do", target="admrul", ID=row["_id"])
            d = {}
            for tag in ("행정규칙명", "행정규칙종류", "발령일자", "발령번호", "소관부처명", "담당부서기관명",
                        "시행일자", "제개정구분명", "제개정이유내용", "조문내용", "현행여부"):
                m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
                if m:
                    d[tag] = _strip_tags(_strip_cdata(m.group(1))).strip()
            if not d.get("조문내용") and not d.get("행정규칙명"):
                raise LawNotFound(f"행정규칙 본문 없음 (일련번호 {row['_id']})")
            return d

        def mapper(row, b):
            kind, no = b.get("행정규칙종류", ""), b.get("발령번호", "")
            doc_no = f"{kind} 제{no}호" if kind and no else (no or "")
            return dict(title=b.get("행정규칙명", ""), doc_no=doc_no, doc_date=b.get("발령일자", ""),
                        category=kind, org=b.get("소관부처명", "고용노동부"),
                        summary=(b.get("제개정이유내용") or "")[:600], body=b.get("조문내용", ""),
                        extra={k: b[k] for k in ("담당부서기관명", "시행일자", "제개정구분명", "현행여부") if b.get(k)})

        return self._batch("admrul", rows, fetch, mapper)

    # ---------- moel.go.kr 빠른인터넷상담 ----------
    def _counsel_store(self, client, row: dict) -> bool:
        """상담 글 1건을 본문까지 받아 저장한다. 아직 답변이 없으면 False (저장하지 않음)."""
        v = client.get(row["id"])
        if not v.get("답변"):
            return False
        body = _join(("질의", v.get("질의")), ("답변", v.get("답변")))
        archive.upsert(self.conn, "counsel", row["id"], title=(row.get("제목") or "")[:300],
                       doc_no=row.get("번호", ""), doc_date=row.get("등록일", ""),
                       org="고용노동부 빠른인터넷상담", summary=(v.get("질의") or "")[:600], body=body)
        return True

    def _counsel_recheck_pending(self, client) -> int:
        """지난 갱신 때 '미완료'라 건너뛴 글을 다시 본다 — 답변이 달렸으면 이제 담는다.

        목록 증분 스캔은 앞쪽 몇 페이지만 보므로, 이 재확인이 없으면 그 글들은 답변이
        달린 뒤에도 영영 들어오지 못한다(갱신마다 수십 건씩 누락).
        """
        waiting = archive.pending(self.conn, "counsel")
        if not waiting:
            return 0
        LOG.info("counsel 재확인 대기 %d건 확인 중", len(waiting))
        added = 0
        for p in waiting:
            row = dict(p["meta"] or {}, id=p["doc_id"])
            try:
                if self._counsel_store(client, row):
                    archive.drop_pending(self.conn, "counsel", p["doc_id"])
                    added += 1
                else:
                    archive.bump_pending(self.conn, "counsel", p["doc_id"])
            except Exception as e:                              # noqa: BLE001
                archive.bump_pending(self.conn, "counsel", p["doc_id"])
                LOG.warning("counsel 재확인 실패 %s: %s", p["doc_id"], str(e)[:100])
        버림 = archive.purge_pending(self.conn, "counsel")
        self.conn.commit()
        LOG.info("counsel 재확인 결과: 새로 담음 %d · 아직 미답변 %d · 포기 %d",
                 added, len(waiting) - added - 버림, 버림)
        return added

    def ingest_counsel(self) -> tuple:
        from moel_fastcounsel import FastCounselClient, FastCounselUpstreamError
        client = FastCounselClient(min_interval=self.throttle.interval)
        known = set() if self.full else archive.known_ids(self.conn, "counsel")
        added = failed = 0
        page, total_pages = 1, None
        t0 = time.time()
        if not self.full:
            added += self._counsel_recheck_pending(client)
        while True:
            try:
                res = client.list(page=page, unit=50)
            except FastCounselUpstreamError as e:
                LOG.error("counsel 목록 p%d 실패: %s", page, e)
                failed += 1
                if failed > 10:
                    raise
                page += 1
                continue
            if total_pages is None:
                total_pages = ((res["total"] or 0) + 49) // 50
                LOG.info("counsel 목록 전체 %s건 (%d페이지)", res["total"], total_pages)
            미답변 = [r for r in res["items"]
                    if r["id"] not in known and r.get("답변여부", "") == "미완료"]
            for r in 미답변:
                # 지금은 본문이 없다 — 다음 갱신 때 다시 보도록 적어 둔다
                archive.add_pending(self.conn, "counsel", r["id"],
                                    {k: r.get(k, "") for k in ("제목", "번호", "등록일")})
            new_rows = [r for r in res["items"] if r["id"] not in known and r.get("답변여부", "") != "미완료"]
            if not res["items"]:
                break
            if not new_rows and not self.full and page > 1:
                LOG.info("counsel p%d: 새 글 없음 — 증분 종료 (미답변 대기 %d건은 기록됨)",
                         page, len(미답변))
                break
            for r in new_rows:
                try:
                    if not self._counsel_store(client, r):
                        # 목록엔 '답변완료'인데 본문이 비어 있는 경우 — 다음에 다시 본다
                        archive.add_pending(self.conn, "counsel", r["id"],
                                            {k: r.get(k, "") for k in ("제목", "번호", "등록일")})
                        continue
                except Exception as e:                          # noqa: BLE001
                    failed += 1
                    LOG.error("counsel %s 본문 실패: %s", r["id"], str(e)[:120])
                    archive.add_pending(self.conn, "counsel", r["id"],
                                        {k: r.get(k, "") for k in ("제목", "번호", "등록일")})
                    continue
                added += 1
                known.add(r["id"])
                if added % 50 == 0:
                    self.conn.commit()
                if added % 500 == 0:
                    LOG.info("counsel 진행: 추가 %d · 실패 %d · p%d/%s · %.0f초", added, failed, page, total_pages, time.time() - t0)
                if self.limit and added >= self.limit:
                    self.conn.commit()
                    LOG.info("counsel --limit %d 도달, 중단", self.limit)
                    return added, failed
            if total_pages and page >= total_pages or (self.max_pages and page >= self.max_pages):
                break
            page += 1
        self.conn.commit()
        return added, failed

    # ---------- 근로기준법 질의회시집 PDF ----------
    def ingest_qnabook(self) -> tuple:
        import qna_pdf
        path = Path(self.pdf_path) if self.pdf_path else DATA_DIR / "근로기준법_질의회시집_2018-2023.pdf"
        if not path.exists():
            LOG.info("PDF 내려받기 → %s", path)
            path.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(qna_pdf.DOWNLOAD_URL, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise LawUpstreamError("PDF가 아닌 응답 — moel.go.kr 게시물 주소가 바뀐 듯함")
            path.write_bytes(r.content)
        entries = qna_pdf.parse_pdf(path)
        LOG.info("qnabook 파싱: %d개 항목", len(entries))
        added, seen = 0, {}
        for e in entries:
            doc_id = e["doc_no"] or f"p{e['page']}"
            if doc_id in seen:                                 # 같은 문서번호가 두 항목에 붙은 경우
                seen[doc_id] += 1
                doc_id = f"{doc_id}#{seen[doc_id]}"
            else:
                seen[doc_id] = 1
            if archive.upsert(self.conn, "qnabook", doc_id, title=e["title"][:300], doc_no=e["doc_no"],
                              doc_date=e["date"], category=e["chapter"], org="고용노동부 근로기준정책과",
                              summary="", body=e["body"], extra={"page": e["page"]}):
                added += 1
        self.conn.commit()
        return added, 0

    # ---------- 근로복지공단 산재판례 (data.go.kr) ----------
    def ingest_comwel(self) -> tuple:
        from comwel import ComwelClient
        client = ComwelClient()
        known = set() if self.full else archive.known_ids(self.conn, "comwel")
        added = page = 0
        total = None
        while True:
            page += 1
            self.throttle.wait()
            res = client.search(page=page, rows=100, max_chars=10_000_000)
            if total is None:
                total = res["total"]
                LOG.info("comwel 전체 %d건 (%d페이지)", total, (total + 99) // 100)
            if not res["items"]:
                break
            for i, it in enumerate(res["items"]):
                doc_id = it["사건번호"] or f"p{page}-{i}"
                if doc_id in known:
                    continue
                cat = " / ".join(x for x in (it.get("사건유형"), it.get("사고질병구분")) if x)
                # 이 API는 선고일자를 주지 않는다 — 사건번호의 접수연도만 뽑아 '추정'으로 표시한다
                # (판결문 본문에도 선고일이 남아 있는 건은 20% 남짓이라 신뢰할 수 없다)
                y = comwel_year(it["사건번호"])
                if archive.upsert(self.conn, "comwel", doc_id, title=it.get("사건명", ""), doc_no=it["사건번호"],
                                  doc_date=f"{y}0101" if y else "", date_kind="사건번호 접수연도" if y else "",
                                  category=cat, org=it.get("법원명", ""), body=it.get("판결문", ""),
                                  extra={"사건결과": it.get("사건결과", "")}):
                    added += 1
                known.add(doc_id)
                if self.limit and added >= self.limit:
                    self.conn.commit()
                    return added, 0
            self.conn.commit()
            if page * 100 >= total or (self.max_pages and page >= self.max_pages):
                break
        return added, 0


def _join(*pairs) -> str:
    return "\n\n".join(f"[{k}]\n{v}" for k, v in pairs if v)


_CASE_YEAR_RE = re.compile(r"(?:\([^)]*\))?\s*(19\d{2}|20\d{2})[가-힣]")


def comwel_year(case_no: str) -> str:
    """산재판례 사건번호에서 접수연도를 뽑는다 — '2019두53822'·'(창원)2018누10258' → '2019'/'2018'.
    선고일이 아니라 접수연도이므로 호출부는 반드시 '추정'으로 표시해야 한다."""
    m = _CASE_YEAR_RE.match(str(case_no or "").strip())
    if not m:
        return ""
    y = int(m.group(1))
    return str(y) if 1960 <= y <= _date.today().year else ""


SOURCE_ORDER = ["admrul", "eiac", "iaciac", "moel", "nlrc", "decc", "qnabook", "counsel", "comwel"]


def run_source(runner: Runner, code: str) -> tuple:
    if code in ("nlrc", "eiac", "iaciac"):
        return runner.ingest_committee(code)
    return {"decc": runner.ingest_decc, "moel": runner.ingest_moel, "admrul": runner.ingest_admrul,
            "counsel": runner.ingest_counsel, "qnabook": runner.ingest_qnabook,
            "comwel": runner.ingest_comwel}[code]()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", help="자료원 코드 또는 all")
    ap.add_argument("--full", action="store_true", help="기존 문서도 다시 받아 갱신")
    ap.add_argument("--limit", type=int, default=0, help="자료원당 새 문서 N건에서 중단 (시험용)")
    ap.add_argument("--pages", type=int, default=0, help="목록 최대 페이지 수 (시험용)")
    ap.add_argument("--interval", type=float, default=0.3, help="요청 간격(초)")
    ap.add_argument("--db", default="", help="DB 경로 (기본 data/labor_archive.sqlite 또는 LABOR_ARCHIVE_DB)")
    ap.add_argument("--pdf", default="", help="질의회시집 PDF 경로")
    ap.add_argument("--stats", action="store_true", help="적재 현황만 출력")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    logging.getLogger("nlrc").setLevel(logging.WARNING)
    db = Path(a.db) if a.db else None
    if a.stats:
        conn = archive.open_db(db, readonly=True)
        print(json.dumps(archive.stats(conn), ensure_ascii=False, indent=1))
        return 0
    codes = SOURCE_ORDER if (not a.sources or "all" in a.sources) else archive.resolve_sources(a.sources)
    conn = archive.open_db(db)
    runner = Runner(conn, interval=a.interval, full=a.full, limit=a.limit, max_pages=a.pages, pdf_path=a.pdf)
    LOG.info("아카이브 적재 시작: %s → %s", ", ".join(codes), archive.db_path() if not db else db)
    rc = 0
    for code in codes:
        t0 = time.time()
        try:
            added, failed = run_source(runner, code)
            status = "OK" if not failed else f"OK(실패 {failed})"
            archive.set_state(conn, code, status, added, f"{time.time() - t0:.0f}초")
            LOG.info("=== %s 완료: 추가 %d · 실패 %d · %.0f초", code, added, failed, time.time() - t0)
        except KeyboardInterrupt:
            conn.commit()
            archive.set_state(conn, code, "INTERRUPTED", 0, "사용자 중단")
            LOG.warning("=== %s 중단(Ctrl+C) — 지금까지 저장분은 커밋됨", code)
            return 130
        except Exception as e:                                  # noqa: BLE001
            conn.commit()
            rc = 1
            archive.set_state(conn, code, f"ERROR:{type(e).__name__}", 0, str(e)[:300])
            LOG.exception("=== %s 실패: %s", code, e)
    print(json.dumps(archive.stats(conn), ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
