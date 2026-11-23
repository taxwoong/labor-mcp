# -*- coding: utf-8 -*-
"""
comwel.py — 근로복지공단 산재보험 판례 판결문 조회 서비스 (data.go.kr 15041878) 클라이언트

엔드포인트(공공데이터포털 스웨거 실측, 2026-09-03):
  https://apis.data.go.kr/B490001/sjbPrecedentInfoService/
    getSjbSageonYuhyeongPstate          사건유형 목록 (보험료·유족·장해 등)      → item.kindb
    getSjbPrecedentResultYuhyeongPstate 판결결과 유형 목록 (기각·취하 등)        → item.kinda
    getSjbSagoJilbyeongGubunPstate      사고/질병 구분 목록                     → item.kindc
    getSjbYuhyeongByCountPstate         유형별 건수 (kindA/kindB/kindC 필터)    → item.cnt
    getSjbPrecedentNaeyongPstate        판결문 내용 (kindA/kindB/kindC 필터)
        → item.accnum(사건번호) courtname(법원명) kinda kindb kindc title(사건명) noncontent(판결문)
  공통 파라미터: ServiceKey, pageNo, numOfRows(필수). 수록기간 2004.01~2023.12.

**키워드 검색이 없다** — 유형 필터와 페이지만 있다. 그래서 실시간 도구는 유형 탐색용이고,
본문 키워드 검색은 아카이브(archive.py)에 전수 적재한 뒤 labor_archive_search로 한다.

인증: 환경변수 DATA_GO_KR_KEY (공공데이터포털 일반 인증키, 디코딩 값). 계정 키는 서비스마다
'활용신청'을 해야 그 서비스에서 유효해진다 — 미신청 키는 SERVICE_KEY_IS_NOT_REGISTERED_ERROR
(returnReasonCode 30)로 거절된다 (2026-09-03 실측). 일일 트래픽 개발계정 10,000건.

v1.5 — 같은 기관의 결정문 서비스 2종을 더했다 (2026-11-23 활용신청·실호출 확인):
  https://apis.data.go.kr/B490001/simsaPstateInfoService/      심사결정서 (data.go.kr 15110818)
    getSimsaResultYuhyeongPstate 결정결과(kinda) · getChungguYuhyeongPstate 청구유형(kindb) ·
    getJaengjeomGubunPstate 쟁점구분(kindc) · getSimsaYuhyeongByCountPstate 건수 ·
    getSimsaResultNaeyongPstate 결정문 → accnum(사건번호) title(사건명) kinda/b/c noncontent(전문)
    — 3,000건, 사건번호가 전부 2017년 접수분(공단 공개 표본).
  https://apis.data.go.kr/B490001/jilbyeongPstateInfoService/  질병판정서 (data.go.kr 15110836)
    getSimuiResultYuhyeongPstate 심의결과(kinda) · getJikjongYuhyeongPstate 직종(kindb) ·
    getJilbyeongGubunPstate 질병구분(kindc) · getJilbyeongYuhyeongByCountPstate 건수 ·
    getJilbyeongResultNaeyongPstate 판정서 → accnum(판정번호) title(**업종**) kinda/b/c noncontent
    — 61,566건, 판정번호 5~8자리가 접수연도(2015~2021).
  **필터 파라미터가 판례 API와 다르다** — 판례는 kindA/kindB/kindC, 이 둘은 소문자 kinda/kindb/kindc다.
  대문자로 보내면 오류 없이 필터를 무시하고 전체를 돌려준다(실측). 둘 다 **일자 필드가 없다** —
  심사결정서는 문서 끝 "2017년 11월 일 근로복지공단 이사장"의 결정 연월만, 질병판정서는 청구취지의
  판정 요청일만 본문에 남아 있어 그것을 뽑아 쓴다(decision_date 참조).
"""
import html
import os
import re
import urllib.parse
from typing import Optional

from datetime import date as _date

import requests

BASE = "https://apis.data.go.kr/B490001/sjbPrecedentInfoService"
SIMSA_BASE = "https://apis.data.go.kr/B490001/simsaPstateInfoService"
JILBYEONG_BASE = "https://apis.data.go.kr/B490001/jilbyeongPstateInfoService"
TIMEOUT = 60
USER_AGENT = "labor-mcp/1.5"


def api_key() -> str:
    """공공데이터포털은 '인코딩 키'(%2B·%3D 포함, 102자)와 '디코딩 키'(88자) 두 가지를 준다.
    requests가 파라미터를 다시 인코딩하므로 디코딩 키를 보내야 한다 — 인코딩 키가 들어오면
    (2026-09-03 실측: 그대로 보내면 SERVICE_KEY_IS_NOT_REGISTERED_ERROR) 여기서 풀어 준다."""
    k = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if "%" in k:
        k = urllib.parse.unquote(k)
    return k


class ComwelError(Exception):
    """공통 기반."""


class ComwelAuthError(ComwelError):
    """키 미설정·미등록·활용신청 누락·트래픽 초과 — 자료 부존재와 무관."""


class ComwelUpstreamError(ComwelError):
    """네트워크·HTTP 오류·서비스 폐기 응답."""


class ComwelParseError(ComwelError):
    """응답 형상이 예상과 달라 해석 실패."""


# data.go.kr 공통 오류 봉투 코드 (OpenAPI_ServiceResponse/cmmMsgHeader/returnReasonCode)
_AUTH_CODES = {"30", "31", "32", "33", "22", "20", "21"}   # 키 오류·미등록·기간만료·트래픽 초과·접근거부
_ERR_RE = re.compile(r"<errMsg>(.*?)</errMsg>.*?<returnAuthMsg>(.*?)</returnAuthMsg>.*?"
                     r"<returnReasonCode>(.*?)</returnReasonCode>", re.S)


def _text(tag: str, block: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    if not m:
        return ""
    v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    v = html.unescape(v)
    v = re.sub(r"<br\s*/?>", "\n", v, flags=re.I)
    v = re.sub(r"<[^>]+>", "", v)
    return v.strip()


def _check_error(text: str) -> None:
    if "OpenAPI_ServiceResponse" not in text[:400]:
        return
    m = _ERR_RE.search(text)
    msg = f"{m.group(1)} / {m.group(2)} (code {m.group(3)})" if m else text[:200]
    code = m.group(3).strip() if m else ""
    if code in _AUTH_CODES:
        raise ComwelAuthError(
            f"data.go.kr 인증 거부: {msg} — DATA_GO_KR_KEY 확인 및 공공데이터포털에서 해당 "
            "근로복지공단 서비스(산재보험 판례 판결문 15041878 · 심사결정서 15110818 · "
            "질병판정서 15110836) 활용신청이 필요합니다. 이것은 자료가 없다는 뜻이 아닙니다.")
    raise ComwelUpstreamError(f"data.go.kr 오류 응답: {msg} — 자료 부존재와 무관합니다.")


def _call(op: str, base: str = BASE, **params) -> str:
    key = api_key()
    if not key:
        raise ComwelAuthError(
            "DATA_GO_KR_KEY 환경변수가 없습니다 — 공공데이터포털 인증키를 local_env.bat에 "
            "설정하세요 (활용신청: data.go.kr/data/15041878/openapi.do). 자료 부존재와 무관합니다.")
    p = {"ServiceKey": key, "pageNo": 1, "numOfRows": 10}
    p.update({k: v for k, v in params.items() if v not in (None, "")})
    try:
        r = requests.get(f"{base}/{op}", params=p, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
    except requests.exceptions.RequestException as e:
        raise ComwelUpstreamError(f"data.go.kr 접속 실패: {type(e).__name__}: {e}") from e
    text = r.text
    _check_error(text)
    if r.status_code != 200:
        raise ComwelUpstreamError(f"data.go.kr HTTP {r.status_code}: {text[:200]}")
    rc = _text("resultCode", text)
    if rc and rc not in ("00", "0", "000"):
        raise ComwelUpstreamError(f"data.go.kr resultCode {rc}: {_text('resultMsg', text)}")
    return text


def _items(text: str) -> list:
    return re.findall(r"<item>(.*?)</item>", text, re.S)


def _total(text: str) -> int:
    t = _text("totalCount", text)
    return int(t) if t.isdigit() else 0


class ComwelClient:
    """산재보험 판례 유형 목록·판결문 조회."""

    def codes(self, rows: int = 200) -> dict:
        """사건결과·사건유형·사고질병구분 코드 목록 (판결문 조회의 필터 값)."""
        out = {}
        for key, op, tag in (("사건결과", "getSjbPrecedentResultYuhyeongPstate", "kinda"),
                             ("사건유형", "getSjbSageonYuhyeongPstate", "kindb"),
                             ("사고질병구분", "getSjbSagoJilbyeongGubunPstate", "kindc")):
            text = _call(op, pageNo=1, numOfRows=rows)
            vals = [_text(tag, b) for b in _items(text)]
            out[key] = [v for v in vals if v]
        return out

    def count(self, result_type: str = "", case_type: str = "", injury_type: str = "") -> int:
        text = _call("getSjbYuhyeongByCountPstate", pageNo=1, numOfRows=1,
                     kindA=result_type, kindB=case_type, kindC=injury_type)
        blocks = _items(text)
        c = _text("cnt", blocks[0]) if blocks else _text("totalCount", text)
        return int(c) if c.isdigit() else 0

    def search(self, result_type: str = "", case_type: str = "", injury_type: str = "",
               page: int = 1, rows: int = 10, max_chars: int = 6000) -> dict:
        """판결문 목록·내용. 필터는 codes()가 돌려준 값을 그대로 넣는다."""
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")
        rows = max(1, min(int(rows), 100))
        text = _call("getSjbPrecedentNaeyongPstate", pageNo=page, numOfRows=rows,
                     kindA=result_type, kindB=case_type, kindC=injury_type)
        blocks = _items(text)
        if not blocks and "<body>" not in text and "<items" not in text:
            raise ComwelParseError(f"응답 형상 해석 실패 — 앞부분: {text[:200]}")
        items = []
        for b in blocks:
            body = _text("noncontent", b)
            row = {"사건번호": _text("accnum", b), "법원명": _text("courtname", b),
                   "사건명": _text("title", b), "사건결과": _text("kinda", b),
                   "사건유형": _text("kindb", b), "사고질병구분": _text("kindc", b)}
            if len(body) > max_chars:
                row["판결문"] = body[:max_chars]
                row["잘림"] = f"판결문 전체 {len(body)}자 중 앞 {max_chars}자 — max_chars를 늘려 재조회"
            else:
                row["판결문"] = body
            items.append(row)
        return {"total": _total(text), "page": page, "items": items}


# ---------------------------------------------------------------------------
# v1.5 — 심사결정서 · 질병판정서
# ---------------------------------------------------------------------------

# 종류 → 서비스 명세. fields는 (번호, 제목, kinda, kindb, kindc)에 붙일 이름.
DECISION_KINDS = {
    "심사결정서": dict(
        base=SIMSA_BASE, list_op="getSimsaResultNaeyongPstate", count_op="getSimsaYuhyeongByCountPstate",
        codes=(("결정결과", "getSimsaResultYuhyeongPstate", "kinda"),
               ("청구유형", "getChungguYuhyeongPstate", "kindb"),
               ("쟁점구분", "getJaengjeomGubunPstate", "kindc")),
        fields=("사건번호", "사건명", "결정결과", "청구유형", "쟁점구분"), body="결정문",
        org="근로복지공단 (산재보험 심사청구)",
        source="근로복지공단 산재보험 심사결정서 (data.go.kr 15110818)"),
    "질병판정서": dict(
        base=JILBYEONG_BASE, list_op="getJilbyeongResultNaeyongPstate",
        count_op="getJilbyeongYuhyeongByCountPstate",
        codes=(("판정결과", "getSimuiResultYuhyeongPstate", "kinda"),
               ("직종", "getJikjongYuhyeongPstate", "kindb"),
               ("질병구분", "getJilbyeongGubunPstate", "kindc")),
        fields=("판정번호", "업종", "판정결과", "직종", "질병구분"), body="판정서",
        org="근로복지공단 업무상질병판정위원회",
        source="근로복지공단 업무상질병판정위원회 질병판정서 (data.go.kr 15110836)"),
}
DECISION_ALIASES = {"심사결정서": "심사결정서", "심사": "심사결정서", "심사결정": "심사결정서", "simsa": "심사결정서",
                    "질병판정서": "질병판정서", "질병판정": "질병판정서", "질판위": "질병판정서",
                    "업무상질병판정위원회": "질병판정서", "jilbyeong": "질병판정서"}


def resolve_kind(kind: str) -> str:
    k = DECISION_ALIASES.get((kind or "").strip().replace(" ", ""))
    if not k:
        raise ValueError(f"kind는 '심사결정서' 또는 '질병판정서'여야 합니다: {kind!r}")
    return k


def _valid_ymd(y: int, m: int, d: int = 1) -> bool:
    return 2000 <= y <= _date.today().year and 1 <= m <= 12 and 1 <= d <= 31


def decision_date(kind: str, accnum: str, body: str) -> tuple:
    """(ISO 일자, 일자근거). 원천에 일자 필드가 없어 본문에서 뽑는다 — 무엇에서 뽑았는지를
    반드시 같이 돌려줘서 화면에 '확정 판정일'처럼 보이지 않게 한다.

    - 심사결정서: 끝의 "2017년 11월 일 근로복지공단 이사장" → (2017-11-01, '결정연월')
      일(日)은 원문에서도 비어 있다.
    - 질병판정서: 청구취지 "…(2015.04.14.)에 따른 판정 요청" → (2015-04-14, '판정요청일')
      판정은 그 뒤(보통 1~2개월)에 난다.
    - 못 뽑으면 번호의 접수연도 → (YYYY-01-01, '사건번호 접수연도'), 그것도 없으면 ('', '').
    """
    text = re.sub(r"\s+", " ", body or "")
    acc = re.sub(r"\D", "", accnum or "")
    if kind == "심사결정서":
        m = None
        for m in re.finditer(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(?:\d{1,2}\s*)?일?\s*근\s*로\s*복\s*지\s*공\s*단",
                             text):
            pass
        if m and _valid_ymd(int(m.group(1)), int(m.group(2))):
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01", "결정연월"
        year = acc[:4]
    else:
        head = text[text.find("청구취지"):][:400] if "청구취지" in text else text[:600]
        year = acc[4:8]
        # 두 가지 표기: "…(2015.04.14.)에 따른 판정 요청" / "심의의뢰처리일자:2019.09.30".
        # 2017~2019년분 상당수는 요청 문서번호·일자가 '(기타 개인정보 생략)'으로 가려져 날짜가 아예 없다
        # (2026-11-23 실측 61,566건 중 약 2.4만 건) — 그때는 접수연도로 떨어지는 것이 정상이다.
        m = (re.search(r"\(\s*(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*\)", head)
             or re.search(r"심의\s*의뢰\S{0,6}일자\s*[:：]?\s*(\d{4})\s*[.-]\s*(\d{1,2})\s*[.-]\s*(\d{1,2})", head))
        # 괄호 날짜가 진단일·입사일 같은 다른 날짜일 수 있다 — 판정번호의 접수연도 ±1년만 받는다
        # (2026-11-23 실측: 2015년 접수 건에서 2008년 날짜가 잡힌 사례)
        near = (not year.isdigit()) or (m and abs(int(m.group(1)) - int(year)) <= 1)
        if m and near and _valid_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3))):
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "판정요청일"
    if year.isdigit() and _valid_ymd(int(year), 1):
        return f"{year}-01-01", "사건번호 접수연도"
    return "", ""


def date_label(iso: str, kind: str) -> str:
    """사람이 읽는 일자 표기 — 근거가 드러나게."""
    if not iso:
        return "미상"
    if kind == "결정연월":
        return f"{iso[:4]}년 {int(iso[5:7])}월 (결정연월 — 일 미공개)"
    if kind == "판정요청일":
        return f"{iso} (판정요청일 — 판정일은 그 이후)"
    if kind:
        return f"{iso[:4]}년경 ({kind} — 확정 일자 아님)"
    return iso


def order_line(body: str) -> str:
    """'주문 … 청구취지' 사이의 주문 문장 — 요지 대용."""
    text = re.sub(r"\s+", " ", body or "").lstrip("？? ")
    m = re.search(r"주\s*문\s*(.+?)(?:청구\s*취지|신청\s*내용|이\s*유)", text)
    return (m.group(1) if m else text[:200]).strip()[:600]


class ComwelDecisionClient:
    """심사결정서 · 질병판정서 조회 (kind로 고른다). 키워드 검색은 없다 — 유형 필터·페이지뿐."""

    def __init__(self, kind: str):
        self.kind = resolve_kind(kind)
        self.spec = DECISION_KINDS[self.kind]

    def codes(self, rows: int = 300) -> dict:
        out = {}
        for label, op, tag in self.spec["codes"]:
            text = _call(op, base=self.spec["base"], pageNo=1, numOfRows=rows)
            out[label] = [v for v in (_text(tag, b) for b in _items(text)) if v]
        return out

    def count(self) -> int:
        """전체 건수. **이 서비스의 건수 오퍼레이션은 필터를 무시한다**(실측) — 필터별 건수는 search()의 total."""
        text = _call(self.spec["count_op"], base=self.spec["base"], pageNo=1, numOfRows=1)
        blocks = _items(text)
        v = _text("cnt", blocks[0]) if blocks else _text("totalCount", text)
        return int(v) if v.isdigit() else 0

    def search(self, a: str = "", b: str = "", c: str = "", page: int = 1, rows: int = 10,
               max_chars: int = 6000) -> dict:
        """a/b/c = codes()의 첫째·둘째·셋째 목록 값 (심사결정서: 결정결과·청구유형·쟁점구분,
        질병판정서: 판정결과·직종·질병구분)."""
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")
        rows = max(1, min(int(rows), 100))
        text = _call(self.spec["list_op"], base=self.spec["base"], pageNo=page, numOfRows=rows,
                     kinda=a, kindb=b, kindc=c)
        blocks = _items(text)
        if not blocks and "<body>" not in text and "<items" not in text:
            raise ComwelParseError(f"응답 형상 해석 실패 — 앞부분: {text[:200]}")
        f = self.spec["fields"]
        items = []
        for blk in blocks:
            body = _text("noncontent", blk).lstrip("？? ")
            acc = _text("accnum", blk)
            iso, dk = decision_date(self.kind, acc, body)
            row = {f[0]: acc, f[1]: _text("title", blk), f[2]: _text("kinda", blk),
                   f[3]: _text("kindb", blk), f[4]: _text("kindc", blk),
                   "주문": order_line(body), "일자": date_label(iso, dk), "일자_ISO": iso, "일자근거": dk}
            if len(body) > max_chars:
                row[self.spec["body"]] = body[:max_chars]
                row["잘림"] = f"{self.spec['body']} 전체 {len(body)}자 중 앞 {max_chars}자 — max_chars를 늘려 재조회"
            else:
                row[self.spec["body"]] = body
            items.append(row)
        return {"자료원": self.spec["source"], "total": _total(text), "page": page, "items": items}


if __name__ == "__main__":
    import json
    c = ComwelClient()
    print(json.dumps(c.codes(), ensure_ascii=False, indent=1))
    r = c.search(rows=2, max_chars=300)
    print(json.dumps(r, ensure_ascii=False, indent=1))
