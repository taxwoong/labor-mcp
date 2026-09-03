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

주의: 성공 응답 형상은 스웨거 스키마 기준으로 작성했고 아직 실호출로 확인하지 못했다
(키 활용신청 대기). 키를 넣은 뒤 `python comwel.py`로 먼저 확인할 것.
"""
import html
import os
import re
import urllib.parse
from typing import Optional

import requests

BASE = "https://apis.data.go.kr/B490001/sjbPrecedentInfoService"
TIMEOUT = 30
USER_AGENT = "labor-mcp/1.2"


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
            f"data.go.kr 인증 거부: {msg} — DATA_GO_KR_KEY 확인 및 공공데이터포털에서 "
            "'근로복지공단_산재보험 판례 판결문 조회 서비스' 활용신청이 필요합니다. "
            "이것은 자료가 없다는 뜻이 아닙니다.")
    raise ComwelUpstreamError(f"data.go.kr 오류 응답: {msg} — 자료 부존재와 무관합니다.")


def _call(op: str, **params) -> str:
    key = api_key()
    if not key:
        raise ComwelAuthError(
            "DATA_GO_KR_KEY 환경변수가 없습니다 — 공공데이터포털 인증키를 local_env.bat에 "
            "설정하세요 (활용신청: data.go.kr/data/15041878/openapi.do). 자료 부존재와 무관합니다.")
    p = {"ServiceKey": key, "pageNo": 1, "numOfRows": 10}
    p.update({k: v for k, v in params.items() if v not in (None, "")})
    try:
        r = requests.get(f"{BASE}/{op}", params=p, timeout=TIMEOUT,
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


if __name__ == "__main__":
    import json
    c = ComwelClient()
    print(json.dumps(c.codes(), ensure_ascii=False, indent=1))
    r = c.search(rows=2, max_chars=300)
    print(json.dumps(r, ensure_ascii=False, indent=1))
