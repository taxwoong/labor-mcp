# -*- coding: utf-8 -*-
"""
server.py — 노무 특화 MCP 서버 (labor-mcp)

목표 기능 5가지 (개발계획.md):
  1. 근로계약서 검토·작성   2. 취업규칙 검토·작성
  3. 노동법 문제 해결방안 도출 (법령·판례·행정해석·판정례 근거)
  4. 임금대장 분석 (최저임금·통상/평균임금·주52시간·가산수당·기재사항)
  5. 급여테이블 설계 (조건 입력 → 법 준수 급여 구조 역산)

구성: 검색 도구 4 + 계산 도구 8 + 분석·설계 도구 2 + 리소스 열람 도구 1 = 15개
      + 정적 리소스 11종(labor:// URI) + 검토 프롬프트 2종

역할 분담 원칙: 근로계약서·취업규칙 같은 '문서'는 Claude가 직접 읽고 판단하며
이 서버는 체크리스트·근거·계산만 공급한다. 반대로 임금대장 같은 '표 데이터'는
정규화 스키마(JSON)로 받아 서버가 결정론적으로 일괄 판정한다.

로컬 실행:
    run_server.bat  (PORT=8735, local_env.bat에서 LAW_API_OC 로드)
배포: 서버컴퓨터 상시 구동 + tailscale funnel --bg --https=8443 localhost:8735
"""
import logging
import re
import os
from datetime import date
import functools
from pathlib import Path
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# law_go_kr는 import 시점에 LAW_API_OC 환경변수를 읽는다 —
# run_server.bat이 local_env.bat을 먼저 call 하므로 프로세스 기동 시 이미 설정돼 있다.
from law_go_kr import (
    LawAuthError,
    LawGoKrClient,
    LawGoKrError,
    LawInvalidInput,
    LawNotFound,
    LawUpstreamError,
)
from moel_expc import MoelExpcClient
from nlrc import NlrcClient, NlrcParseError, NlrcUpstreamError
import calculators as calc
import payroll as pr
import labor_constants as LC

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

PORT = int(os.environ.get("PORT", 8735))
BASE_DIR = Path(__file__).parent
RESOURCE_DIR = BASE_DIR / "resources"

mcp = FastMCP(
    "labor-mcp",
    instructions=(
        "대한민국 노무(노동법) 특화 도구입니다. "
        "법령 조문·부칙·연혁은 labor_law_article, 고용노동부 행정해석(질의회시)과 법제처 "
        "법령해석례는 moel_interpretation_search, 법원 노동판례는 labor_case_search, "
        "노동위원회 판정례는 nlrc_decision_search를 사용하세요. "
        "수치 검증(최저임금·통상/평균임금·연차·주휴·가산수당·퇴직급여·해고예고)은 계산 도구를, "
        "임금대장 전체 점검은 analyze_payroll(labor_resource('schema/임금대장-입력')의 스키마로 "
        "변환 후 호출), 급여 구조 설계는 design_pay_table을 사용하세요. "
        "근로계약서·취업규칙 검토 시에는 먼저 labor_resource('checklist/근로계약서' 또는 "
        "'checklist/취업규칙')를 로드해 체크리스트 순서대로 검토하세요 "
        "(표준취업규칙은 약 6만 자이므로 chapter 인자로 장 단위로 나눠 읽을 것). "
        "답변 초안에 인용한 문서번호(판례·행정해석·판정례)는 verify_citations로 실존 여부를 "
        "일괄 검증할 수 있습니다. 검색 결과가 이상하면 check_sources_health로 원천 사이트 "
        "상태를 먼저 확인하세요. "
        "계산 결과의 최종 판단에는 전문가(공인노무사) 확인이 필요함을 항상 안내하세요.\n\n"
        "모든 도구 응답의 status 필드 해석: "
        "OK(정상) · NOT_FOUND(검색은 성공했고 결과 없음 — 자료 부존재로 판단해도 됨) · "
        "UPSTREAM_ERROR(원천 사이트 접근 실패) · PARSE_ERROR(사이트 개편 등으로 응답 해석 실패) — "
        "**이 둘은 자료가 없다는 뜻이 절대 아니므로 부존재로 단정하지 말고 사용자에게 조회 "
        "실패를 알릴 것** · AUTH_ERROR(law.go.kr 기관코드·IP 등록 문제 — 부존재와 무관, "
        "서버 관리자 확인 필요) · INVALID_INPUT(입력 형식 오류 — 안내에 따라 고쳐 재호출). "
        "응답에 '안내' 필드가 있으면 그 지시를 따르세요.\n"
        "임금대장을 다룰 때 주민등록번호·계좌번호 등은 입력하지 마세요 — 스키마가 요구하지 "
        "않으며, 입력값은 AI 대화 컨텍스트를 통과합니다."
    ),
    # MCP 스펙은 로컬 바인딩(127.0.0.1)을 권고하지만, **이 서버컴퓨터에서는 쓸 수 없다** —
    # Windows에서 `localhost`가 ::1(IPv6)로 먼저 풀리는데 uvicorn은 주소 하나에만 바인딩되고,
    # Tailscale Funnel의 전달 대상이 `localhost:8735`라 IPv6로 접속해 연결이 거부된다
    # (2026-09-01 실장애: 커넥터 연결 실패). 127.0.0.1로 좁히려면 Funnel 대상을
    #   tailscale funnel --set-path=<비밀경로> 127.0.0.1:8735
    # 로 먼저 바꿔야 한다. HOST 환경변수로 조정 가능.
    host=os.environ.get("HOST", "0.0.0.0"),
    port=PORT,
    stateless_http=False,
)

_law = LawGoKrClient()
_moel = MoelExpcClient()
_nlrc = NlrcClient()


_DISCLAIMER = ("이 계산은 참고용입니다 — 최종 판단은 공인노무사 확인이 필요합니다.")

_GUIDANCE = {
    "NOT_FOUND": "검색·조회는 정상 수행됐고 결과가 0건입니다 — 이 경우에 한해 "
                 "'해당 자료 없음'으로 판단해도 됩니다.",
    "UPSTREAM_ERROR": "원천 사이트 접근에 실패했습니다. **자료가 없다는 뜻이 아닙니다** — "
                      "사용자에게 조회 실패를 알리고, 부존재로 단정하지 마세요.",
    "PARSE_ERROR": "원천 사이트의 화면 구조가 바뀌어 응답을 해석하지 못했습니다. "
                   "**자료가 없다는 뜻이 아닙니다** — 사용자에게 조회 실패를 알리고, "
                   "필요하면 원천 사이트에서 직접 확인하도록 안내하세요.",
    "AUTH_ERROR": "law.go.kr 인증(기관코드·서버 IP 등록) 문제입니다. 자료 부존재와 무관하며 "
                  "서버 관리자 확인이 필요합니다 — 검색 결과 0건으로 해석하지 마세요.",
    "INVALID_INPUT": "입력 형식이 잘못됐습니다 — 오류 메시지의 형식으로 고쳐 다시 호출하세요.",
}


def _status_for(exc: BaseException) -> str:
    if isinstance(exc, LawAuthError):
        return "AUTH_ERROR"
    if isinstance(exc, NlrcParseError):
        return "PARSE_ERROR"
    if isinstance(exc, LawNotFound):
        return "NOT_FOUND"
    if isinstance(exc, (LawInvalidInput, ValueError, KeyError, TypeError, AttributeError)):
        return "INVALID_INPUT"
    if isinstance(exc, (LawUpstreamError, NlrcUpstreamError, LawGoKrError)):
        return "UPSTREAM_ERROR"
    return "UPSTREAM_ERROR"


def _looks_empty(out: dict) -> bool:
    """검색 결과가 0건인지 — 원천 장애와 구분되도록 클라이언트가 이미 검증한 뒤에만 호출."""
    if out.get("total") == 0 or out.get("총건수") == 0:
        return True
    for k in ("items", "cases", "연혁", "고용노동부_행정해석"):
        if k in out and not out[k]:
            if k == "고용노동부_행정해석" and out.get("법제처_법령해석례"):
                return False
            return True
    return False


def _guard(calc_tool: bool = False):
    """모든 도구 응답에 status를 붙이고, 예외를 계약된 오류 응답으로 바꾼다.

    예전에는 requests 예외가 그대로 새어나가 도구마다 다른 형상(ToolError 영문 문자열 /
    빈 결과 / {"오류": ...})으로 나갔고, 원천 장애가 "자료 없음"으로 둔갑했다.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                out = fn(*args, **kwargs)
            except Exception as e:                      # noqa: BLE001 — 계약상 전부 포획
                st = _status_for(e)
                logging.warning("%s 실패(%s): %s", fn.__name__, st, e)
                payload = {"status": st, "오류": f"{type(e).__name__}: {e}",
                           "안내": _GUIDANCE[st]}
                if calc_tool:
                    payload["주의사항"] = [_DISCLAIMER]
                return payload
            if not isinstance(out, dict):
                out = {"결과": out}
            if "status" not in out:
                if "오류" in out:
                    out["status"] = "INVALID_INPUT"
                elif _looks_empty(out):
                    out["status"] = "NOT_FOUND"
                else:
                    out["status"] = "OK"
            if out["status"] in _GUIDANCE:
                out.setdefault("안내", _GUIDANCE[out["status"]])
            if calc_tool:
                주의 = out.setdefault("주의사항", [])
                if isinstance(주의, list) and _DISCLAIMER not in 주의:
                    주의.append(_DISCLAIMER)
            return out
        return wrapper
    return deco


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _resolve_law(name: str) -> tuple:
    """통칭('근기법')을 정식 법령명으로 바꾸고, 알려진 법령ID를 붙여 반환."""
    full = LC.LAW_ALIASES.get(name.strip(), name.strip())
    return full, LC.LAW_IDS.get(full, "")


# ---------------------------------------------------------------------------
# 검색 도구 4종
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def labor_law_article(
    law: str,
    article_no: str = "",
    as_of_date: str = "",
    mode: Literal["조문", "부칙", "연혁"] = "조문",
    promul_no: str = "",
    recent: int = 10,
    max_chars: int = 6000,
) -> dict:
    """노동법령 조문·부칙·연혁 조회 (국가법령정보센터 law.go.kr).

    주요 노동법령은 통칭으로 불러도 된다: 근기법, 기간제법, 파견법, 최저임금법,
    남녀고용평등법, 노조법, 산안법, 퇴직급여법, 외국인고용법. 동명 시행령 혼입을
    막기 위해 알려진 법령은 법령ID 필터가 자동 적용된다.

    Args:
        law: 법령명 (예: "근로기준법", "근기법", "근로기준법 시행령")
        article_no: 조번호 — "17", "76의2" 형식 (mode="조문"에서 필수,
            mode="부칙"에서는 그 조문이 언급된 적용례·경과조치 발췌용 선택 인자)
        as_of_date: 기준일 YYYYMMDD. 생략 시 오늘 — 판례·해석 당시 조문을 보려면
            그 시점 날짜를 지정 (예: 회신일)
        mode: "조문"(기본) | "부칙"(시행일·적용례·경과조치) | "연혁"(전체 시행본 목록)
        promul_no: mode="부칙"에서 특정 개정의 부칙 전문을 볼 때 공포번호
        recent: mode="부칙" 목록 모드의 반환 건수
        max_chars: 본문 최대 길이 (잘리면 응답에 "잘림" 안내 포함)

    Returns:
        조문 원문(조문시행일자·적용시행본 포함) / 부칙 / 연혁 목록.
    """
    try:
        law_name, law_id = _resolve_law(law)
        if mode == "조문":
            if not article_no:
                return {"오류": "mode='조문'에는 article_no가 필요합니다 (예: '17', '76의2')"}
            return _law.law_article_as_of(
                law_name, as_of_date or _today(), article_no, law_id=law_id, max_chars=max_chars)
        if mode == "부칙":
            return _law.law_addenda(
                law_name=law_name, law_id=law_id, as_of_date=as_of_date,
                promul_no=promul_no, article_no=article_no, recent=recent, max_chars=max_chars)
        if mode == "연혁":
            rows = _law.law_history(law_name, law_id=law_id)
            return {"법령명": law_name, "시행본수": len(rows), "연혁": rows}
        return {"오류": f"mode는 조문/부칙/연혁 중 하나여야 합니다 (입력: {mode})"}
    except (LawGoKrError, ValueError):
        raise   # _guard가 status(AUTH_ERROR/NOT_FOUND/UPSTREAM_ERROR/INVALID_INPUT)로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def moel_interpretation_search(
    keyword: str = "",
    serial: str = "",
    display: int = 10,
    date_from: str = "",
    date_to: str = "",
    search_body: bool = False,
    latest_first: bool = False,
    moleg_too: bool = True,
    max_chars: int = 8000,
) -> dict:
    """고용노동부 행정해석(질의회시) + 법제처 법령해석례 검색·본문 조회.

    고용노동부 행정해석은 "근로기준정책과-3084", "근기 68207-2140" 같은 실무
    문서번호 그대로 수록되어 있으며 질의요지·회답 전문을 제공한다. 안건번호
    조각("근로기준정책과", "임금근로시간과")으로도 검색된다.

    Args:
        keyword: 검색어 (serial 미지정 시 필수)
        serial: 일련번호 — 지정하면 그 행정해석의 본문(질의요지·회답 전문) 반환
        display: 결과 수 (기본 10)
        date_from / date_to: 해석일자 범위 YYYYMMDD (서버측 필터)
        search_body: True면 본문 검색, 기본은 안건명 검색
        latest_first: True면 해석일자 최신순 정렬
        moleg_too: True(기본)면 법제처 법령해석례도 함께 검색.
            단 date/sort/본문검색 옵션을 쓰면 고용노동부 쪽만 검색된다.
        max_chars: 본문 조회 시 필드당 최대 길이

    Returns:
        serial 지정 시 본문 dict, 아니면 검색 결과 (고용노동부/법제처 구분).
    """
    try:
        if serial:
            return _moel.get(serial, max_chars=max_chars)
        if not keyword:
            return {"오류": "keyword 또는 serial 중 하나는 필요합니다"}
        filtered = bool(date_from or date_to or search_body or latest_first)
        if moleg_too and not filtered:
            return _moel.search_both(keyword, display=display)
        r = _moel.search(
            keyword, display=display, date_from=date_from, date_to=date_to,
            search_body=search_body, sort="ddes" if latest_first else "")
        # 키 이름을 search_both와 통일한다 — 예전에는 latest_first를 켜는 순간
        # 최상위 키가 total/items로 바뀌어 LLM이 찾던 키가 사라졌다.
        out = {"고용노동부_행정해석": r["items"],
               "고용노동부_행정해석_총건수": r["total"],
               "법제처_법령해석례": [],
               "법제처_생략사유": ("필터·정렬 지정 시에는 고용노동부 행정해석만 검색합니다 "
                             "— 법제처 법령해석례가 필요하면 필터 없이 다시 호출하세요.")}
        if r.get("안내"):
            out["안내"] = r["안내"]
        return out
    except (LawGoKrError, ValueError):
        raise   # _guard가 status(AUTH_ERROR/NOT_FOUND/UPSTREAM_ERROR/INVALID_INPUT)로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def labor_case_search(
    keyword: str = "",
    case_serial: str = "",
    court: str = "",
    date_from: str = "",
    date_to: str = "",
    display: int = 10,
    page: int = 1,
    max_chars: int = 8000,
) -> dict:
    """법원 노동판례 검색·본문 조회 (법제처 판례DB — 대법원·하급심).

    본문 전문검색이라 무관한 판례가 섞일 수 있으니 사건명·사건종류로 걸러 볼 것.
    통상임금·부당해고·취업규칙 불이익변경 등 노동 쟁점 판례가 최신 선고분까지 수록.

    Args:
        keyword: 검색어 (case_serial 미지정 시 필수)
        case_serial: 판례일련번호 — 지정하면 본문(판시사항·판결요지·참조조문·전문) 반환
        court: **법원명 그대로** — "대법원", "서울고등법원" 등. '하위법원'·'하급심'
            같은 분류어는 law.go.kr이 인식하지 못해 항상 0건이라 거부된다 (빈값 = 전체)
        date_from / date_to: 선고일자 범위 YYYYMMDD
        display / page: 페이지네이션
        max_chars: 판례내용 최대 길이
    """
    try:
        if case_serial:
            return _law.get_case(case_serial, max_chars=max_chars)
        if not keyword:
            return {"오류": "keyword 또는 case_serial 중 하나는 필요합니다"}
        return _law.search_cases(
            keyword, court=court, date_from=date_from, date_to=date_to,
            display=display, page=page)
    except (LawGoKrError, ValueError):
        raise   # _guard가 status(AUTH_ERROR/NOT_FOUND/UPSTREAM_ERROR/INVALID_INPUT)로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def nlrc_decision_search(
    keyword: str = "",
    category: str = "",
    page: int = 1,
    date_from: str = "",
    date_to: str = "",
    committee: str = "",
    detail_keys: Optional[dict] = None,
) -> dict:
    """중앙노동위원회(nlrc.go.kr) 판정례·결정요지 검색.

    부당해고·부당노동행위 4만 4천여 건 등 판정요지 전문이 목록에 포함된다.
    카테고리를 1개만 지정하면 페이지네이션(페이지당 10건)이 되고, 복수·전체
    검색은 카테고리당 약 5건 미리보기만 온다(사이트 동작).

    Args:
        keyword: 검색어 (detail_keys 미지정 시 필수 — 사이트가 빈 검색어 미지원)
        category: "부당해고" | "차별시정" | "교섭창구단일화" | "교섭단위" |
            "공정대표" | "기타" (쉼표로 복수 지정 가능, 빈값 = 전체)
        page: 페이지 (단일 카테고리에서만 유효)
        date_from / date_to: 판정일 범위 (YYYYMMDD)
        committee: 관할위원회 필터 (예: "중앙", "서울", "경기")
        detail_keys: 검색 결과 항목의 detail_keys를 그대로 주면 그 사건의
            판정사항·판정요지 상세를 반환 (결정서 전문은 사이트가 미제공)
    """
    try:
        if detail_keys:
            return _nlrc.get_detail(detail_keys)
        if not keyword:
            return {"오류": "keyword 또는 detail_keys 중 하나는 필요합니다"}
        cats = [c.strip() for c in category.split(",") if c.strip()] if category else None
        return _nlrc.search(
            keyword, categories=cats, page=page,
            date_from=date_from, date_to=date_to, committee=committee)
    except Exception:  # requests·파싱 예외 포함 — _guard가 status로 분류한다
        raise


# ---------------------------------------------------------------------------
# 계산 도구 8종 — 반환 공통 규약: {결과, 계산과정, 근거, 주의사항}
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def check_minimum_wage(
    items: list,
    weekly_hours: float = 40.0,
    year: int = 0,
    probation: bool = False,
    contract_1yr_plus: bool = False,
    simple_labor: bool = False,
) -> dict:
    """최저임금 위반 판정 — 산입범위 분해 포함.

    매월 지급분만 산입(격월·분기·연간 상여 불산입), 2019~2023년은 상여·복리후생
    미산입 비율 자동 적용, 2024년 이후 전액 산입. 수습 감액(10%)은 3요건
    (1년 이상 계약 + 3개월 이내 + 단순노무직 아님) 충족 시에만 반영된다.

    Args:
        items: 월 임금항목 목록 — [{"명칭","금액","지급주기","성격","실비변상"?}]
            (성격: 기본급|정기상여|식대|교통비|직책수당|가족수당|기술수당|성과급|
             연장수당|야간수당|휴일수당|연차수당|기타)
        weekly_hours: 주 소정근로시간
        year: 적용 연도 (0이면 올해). 파라미터 테이블에 없는 연도는 명시적 오류
        probation: 수습 3개월 이내 여부
        contract_1yr_plus: 근로계약 기간 1년 이상 여부
        simple_labor: 단순노무직(한국표준직업분류 대분류9) 여부
    """
    try:
        return calc.check_minimum_wage(
            items, weekly_hours, year or date.today().year,
            probation=probation, contract_1yr_plus=contract_1yr_plus,
            simple_labor=simple_labor)
    except ValueError:
        raise   # _guard가 INVALID_INPUT으로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_ordinary_avg_wage(
    items: list,
    weekly_hours: float = 40.0,
    year: int = 0,
    as_of_date: str = "",
    last_3m_items: Optional[list] = None,
    annual_bonus_total: int = 0,
    annual_leave_pay_total: int = 0,
    days_in_window: int = 0,
) -> dict:
    """통상임금(월·시급) 산정 + (선택) 평균임금 산정.

    2024-12-19 대법원 전원합의체 신법리 기준: 재직조건·최소근무일수 조건이 붙은
    정기상여금도 통상임금에 포함(정기성·일률성 기준). 격월·분기 상여도 월 환산
    포함. 평균임금은 last_3m_items를 주면 함께 계산 — 연간 상여·연차수당은
    3/12 산입, 통상임금 하한 비교 포함.

    Args:
        items: 월 임금항목 목록 (통상임금 산정용)
        weekly_hours: 주 소정근로시간
        year: 적용 연도 (0이면 올해)
        as_of_date: 산정 기준일 — 2024-12-19 이전이면 구법리 주의 문구 출력
        last_3m_items: 평균임금용 — 직전 3개월 월별 임금항목 리스트의 리스트
        annual_bonus_total: 연간 상여 총액 (평균임금 3/12 산입용)
        annual_leave_pay_total: 연간 연차수당 총액 (평균임금 3/12 산입용)
        days_in_window: 3개월 창구의 총 일수 (예: 92)
    """
    try:
        y = year or date.today().year
        ow_r = calc.calc_ordinary_wage(items, weekly_hours, y, as_of_date=as_of_date or None)
        결과 = {"통상임금": ow_r["결과"]}
        계산과정 = list(ow_r["계산과정"])
        근거 = list(ow_r["근거"])
        주의 = list(ow_r["주의사항"])
        if last_3m_items:
            if not days_in_window:
                주의.append("days_in_window(산정 창구 총일수)를 지정하지 않아 91일로 가정했습니다 "
                           "— 실제 창구가 89~92일이면 1일 평균임금이 최대 약 2% 달라집니다.")
            aw_r = calc.calc_average_wage(
                last_3m_items, annual_bonus_total, annual_leave_pay_total,
                days_in_window or 91, ordinary_wage_monthly=ow_r["결과"]["월통상임금"],
                weekly_hours=weekly_hours)
            결과["평균임금"] = aw_r["결과"]
            계산과정 += aw_r["계산과정"]
            근거 += [g for g in aw_r["근거"] if g not in 근거]
            주의 += [c for c in aw_r["주의사항"] if c not in 주의]
        else:
            주의.append("평균임금은 last_3m_items(직전 3개월 임금항목)를 넣어야 계산됩니다 "
                       "— 지금 결과에는 통상임금만 있습니다.")
        # 계산 도구 8종의 {결과, 계산과정, 근거, 주의사항} 4필드 규약에 맞춘다
        return {"결과": 결과, "계산과정": 계산과정, "근거": 근거, "주의사항": 주의}
    except ValueError:
        raise   # _guard가 INVALID_INPUT으로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_annual_leave(
    hire_date: str,
    base_date: str,
    attendance_rate: float = 1.0,
    mode: str = "입사일",
    fiscal_start: str = "01-01",
    employed_on_base_date: bool = True,
) -> dict:
    """연차유급휴가 일수 계산 (입사일 기준 / 회계연도 기준).

    함정 반영: 1년 기간제 최대 11일(대법 2021다227100), 연차는 발생일에 재직해야
    인정(행정해석 2021-12-16 변경 — 365일 근무 후 퇴직 시 15일분 미발생),
    회계연도 모드는 퇴직 시 입사일 기준과 비교해 유리한 쪽 보장. 5인 미만 사업장은
    연차 규정 미적용이므로 이 도구를 쓰지 말 것.

    Args:
        hire_date: 입사일 "YYYY-MM-DD"
        base_date: 산정 기준일 "YYYY-MM-DD" (퇴직 정산이면 퇴직일)
        attendance_rate: 직전 연차산정 연도의 출근율 (0.8 미만이면 그 발생분 0)
        mode: "입사일"(기본) | "회계연도"
        fiscal_start: 회계연도 시작일 "MM-DD"
        employed_on_base_date: 기준일 당일 재직 여부 (퇴직 정산이면 False)
    """
    try:
        return calc.calc_annual_leave(
            hire_date, base_date, attendance_rate=attendance_rate, mode=mode,
            fiscal_start=fiscal_start, employed_on_base_date=employed_on_base_date)
    except ValueError:
        raise   # _guard가 INVALID_INPUT으로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_weekly_holiday_pay(
    weekly_hours: float,
    hourly_wage: float,
    perfect_attendance: bool = True,
) -> dict:
    """주휴수당 계산 — 주 15시간 미만 제외, 단시간 비례.

    '다음 주 근로 예정' 요건은 폐지됨(고용부 임금근로시간과-1736, 2021-08-04) —
    1주 개근 + 주휴일까지 근로관계 존속이면 발생. 5인 미만 사업장에도 적용.

    Args:
        weekly_hours: 주 소정근로시간
        hourly_wage: 시급
        perfect_attendance: 소정근로일 개근 여부
    """
    return calc.calc_weekly_holiday_pay(weekly_hours, hourly_wage,
                                        perfect_attendance=perfect_attendance)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_overtime_pay(
    ordinary_hourly: float,
    overtime_h: float = 0,
    night_h: float = 0,
    holiday_h: float = 0,
    holiday_over8_h: float = 0,
    employees: int = 5,
) -> dict:
    """연장·야간·휴일 가산수당 통합 계산 (통상시급 기준, 중복 가산 반영).

    5인 미만 사업장은 가산 의무가 없어 가산분 0으로 계산된다(시간분 100%만).
    연장·휴일수당은 기본 100% 포함 금액(1.5배/2.0배), 야간은 가산 50%만 반환.

    Args:
        ordinary_hourly: 통상시급 (calc_ordinary_avg_wage로 먼저 산출 권장)
        overtime_h: 연장근로 시간
        night_h: 야간근로(22~06시) 시간
        holiday_h: **총** 휴일근로 시간. holiday_over8_h를 함께 주면 그중 8시간
            초과분을 지정하는 것이고, 생략하면 8시간 기준으로 자동 분리한다
        holiday_over8_h: 휴일 8시간 초과분 (holiday_h에서 차감된다)
        employees: 상시 근로자 수 (5 미만이면 가산 미적용) — **지정하지 않으면
            5인(전면 적용)으로 가정하며, 그 사실이 주의사항에 표시된다**
    """
    out = calc.calc_overtime_pay(ordinary_hourly, overtime_h, night_h,
                                 holiday_h, holiday_over8_h=holiday_over8_h,
                                 employees=employees)
    if employees == 5:
        # 5인 미만이면 §56 자체가 적용되지 않아 결론이 정반대가 된다 — 가정을 밝힌다
        out.setdefault("주의사항", []).append(
            "employees를 지정하지 않으면 상시 5인(=§56 전면 적용)으로 가정합니다 — "
            "4인 이하 사업장이면 가산 의무가 없어 결과가 달라지므로 실제 인원을 넣으세요.")
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_severance_pay(
    avg_daily_wage: float,
    service_days: int,
    weekly_hours: float = 40.0,
    plan_type: Literal["퇴직금", "DB", "DC"] = "퇴직금",
) -> dict:
    """퇴직급여 계산 — 1년 미만·주 15시간 미만 제외, DC형 구분.

    평균임금이 통상임금보다 낮으면 통상임금으로 계산해야 함(근기법 2조② —
    calc_ordinary_avg_wage의 '적용평균임금'을 쓸 것). 퇴직금 월급 분할지급
    약정은 무효(대법 전합 2007다90760).

    Args:
        avg_daily_wage: 1일 평균임금 (통상임금 하한 반영값 권장)
        service_days: 계속근로일수
        weekly_hours: 주 소정근로시간 (15시간 미만이면 대상 제외)
        plan_type: "퇴직금"(기본) | "DB" | "DC" — DC형은 계산식이 다름을 안내
    """
    return calc.calc_severance_pay(avg_daily_wage, service_days, weekly_hours,
                                   plan_type=plan_type)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_dismissal_notice_pay(
    ordinary_daily: float,
    service_months: float,
    exempt_reason: str = "",
) -> dict:
    """해고예고수당 계산 — 30일 전 예고 없으면 30일분 이상의 통상임금.

    예외: 계속근로 3개월 미만, 천재·사변, 근로자 귀책(고용노동부령 사유).
    5인 미만 사업장에도 적용. 해고예고가 적법해도 해고의 정당성(근기법 23조)은
    별개로 판단해야 함.

    Args:
        ordinary_daily: 1일 통상임금
        service_months: 계속근로 개월 수
        exempt_reason: 예외 사유가 있으면 기재 (예: "천재사변", "근로자 귀책")
    """
    return calc.calc_dismissal_notice_pay(ordinary_daily, service_months,
                                          exempt_reason=exempt_reason or None)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def calc_wage_cut_limit(avg_daily_wage: float, wage_period_total: float) -> dict:
    """징계 감급(감봉) 한도 계산 (근기법 95조).

    1회의 감급액은 평균임금 1일분의 1/2, 총액은 1임금지급기 임금 총액의 1/10을
    초과할 수 없다. 취업규칙 징계 조항 검토 시 사용.

    Args:
        avg_daily_wage: 1일 평균임금
        wage_period_total: 1임금지급기(통상 월) 임금 총액
    """
    return calc.calc_wage_cut_limit(avg_daily_wage, wage_period_total)


# ---------------------------------------------------------------------------
# 분석·설계 도구 2종
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def analyze_payroll(data: dict) -> dict:
    """임금대장 일괄 분석 — 직원×월 매트릭스로 6종 검사.

    검사: ①최저임금 위반(산입 분해) ②통상임금 산정(2024 전합 신법리)
    ③평균임금(퇴사자) ④주52시간 위반 ⑤가산수당 미지급(이론치 대조)
    ⑥임금대장 기재사항 누락(시행령 27조 — 4인 이하 예외 반영).
    판정은 위반|적법|불확실 3단계 — 데이터 부족·법리 다툼은 불확실로 분리된다.

    입력 스키마는 labor_resource("schema/임금대장-입력")를 먼저 읽고, 엑셀
    임금대장을 그 스키마의 JSON으로 변환해 전달할 것. 형식:
    {"사업장": {"상시근로자수", "연도", "탄력선택근로제"}, "직원": [...]}

    Args:
        data: 정규화된 임금대장 (스키마 문서 참조)
    """
    try:
        return pr.analyze_payroll(data)
    except ValueError:
        raise   # _guard가 INVALID_INPUT으로 분류한다


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard(calc_tool=True)
def design_pay_table(cond: dict) -> dict:
    """급여테이블 역산 설계 — 조건 입력 → 법 준수 급여 구조 + 계약서 임금조항 문안.

    월 목표액을 기본급 + 고정OT수당(통상시급×시간×배율)으로 대수적으로 분해하고,
    최저임금을 역검증한 뒤 근기법 17조② 서면교부 형식의 임금조항 문안까지 생성.
    통상임금 절감 목적의 항목 쪼개기는 2024 전합 이후 실익이 없음을 함께 안내.

    Args:
        cond: {"연도": int(필수), "목표": {"방식": "월총액|기본급|연봉", "금액": int}(필수),
            "상시근로자수": int, "주소정근로시간": float,
            "고정연장시간_월"/"고정야간시간_월"/"고정휴일시간_월": float,
            "고정수당": [임금항목] | null, "정기상여": {"연간총액", "지급주기"} | null,
            "수습적용"/"계약기간1년이상"/"단순노무직": bool}
    """
    try:
        return pr.design_pay_table(cond)
    except ValueError:
        raise   # _guard가 INVALID_INPUT으로 분류한다


# ---------------------------------------------------------------------------
# 지식 리소스 — labor:// URI + 열람 도구
# (claude.ai 커넥터에서 resources가 UI에 노출되지 않는 경우를 대비해
#  labor_resource 도구로도 같은 내용을 열람할 수 있게 이중 노출한다)
# ---------------------------------------------------------------------------

_RESOURCES = {
    "checklist/근로계약서": ("checklist_근로계약서.md", "근로계약서 검토 체크리스트 — 필수기재·위법조항 15유형·유형별 분기"),
    "checklist/취업규칙": ("checklist_취업규칙.md", "취업규칙 검토 체크리스트 — 93조 14항목·불이익변경 판례·신고 실무"),
    "table/임금항목-산입매트릭스": ("임금항목_산입매트릭스.md", "임금항목별 최저임금×통상임금×평균임금 3축 판정표"),
    "table/5인미만-적용제외": ("5인미만_적용제외.md", "4인 이하 사업장 근로기준법 적용/미적용 매트릭스"),
    "table/시행중-개정법-기준선": ("시행중_개정법_기준선.md", "2026-08 기준 시행 중 개정법·판례 기준선 + 추진 중 입법"),
    "table/최저임금-연도별": ("최저임금_연도별.md", "연도별 최저임금·산입범위 스케줄·수습 감액 요건"),
    "schema/임금대장-입력": ("임금대장_입력스키마.md", "analyze_payroll 입력 스키마 + 엑셀 변환 가이드"),
    "template/표준취업규칙-2026": ("표준취업규칙_2026.md", "고용노동부 2026 표준취업규칙 (일반, 17장 98개조, 필수/선택 표기)"),
    "template/표준취업규칙-단시간-2026": ("표준취업규칙_단시간_2026.md", "고용노동부 2026 표준취업규칙 (단시간근로자용, 96개조)"),
    "template/직장내괴롭힘-표준안-2026": ("직장내괴롭힘_표준안_2026.md", "직장 내 괴롭힘 예방·대응 규정 표준안 (20개조)"),
    "template/표준근로계약서": ("표준근로계약서.md", "고용노동부 표준근로계약서 (2025 배포본, 서식 6종+친권자 동의서)"),
}


def _slice_doc(text: str, chapter: str = "", article: str = ""):
    """표준취업규칙류 문서에서 장·조 단위로 잘라낸다 (## 제N장 / ### 제N조 구조)."""
    import re as _re
    if article:
        no = article.strip().lstrip("제").rstrip("조")
        pat = _re.compile(rf"^#+\s*제\s*{_re.escape(no)}\s*조", _re.M)
    else:
        no = chapter.strip().lstrip("제").rstrip("장")
        pat = _re.compile(rf"^#+\s*제\s*{_re.escape(no)}\s*장", _re.M)
    m = pat.search(text)
    라벨 = (f"제{no}조" if article else f"제{no}장")
    if not m:
        return None, 라벨
    level = len(text[m.start():m.end()].split()[0])
    nxt = _re.compile(rf"^#{{1,{level}}}\s", _re.M).search(text, m.end())
    return text[m.start(): nxt.start() if nxt else len(text)], 라벨


def _read_resource(fname: str) -> str:
    return (RESOURCE_DIR / fname).read_text(encoding="utf-8")


# MCP 리소스 URI는 퍼센트 인코딩되어 등록되므로 한글 키를 그대로 쓰면
# labor://checklist/%EA%B7%BC... 가 되어 **문서에 적힌 주소로는 read가 실패**한다
# (2026-09-01 실측). ASCII 슬러그로 등록하고, 도구 쪽 한글 키는 그대로 받는다.
_RESOURCE_SLUGS = {
    "checklist/근로계약서": "checklist/employment-contract",
    "checklist/취업규칙": "checklist/work-rules",
    "table/임금항목-산입매트릭스": "table/wage-item-matrix",
    "table/5인미만-적용제외": "table/under5-exemptions",
    "table/시행중-개정법-기준선": "table/law-baseline",
    "table/최저임금-연도별": "table/minimum-wage-by-year",
    "schema/임금대장-입력": "schema/payroll-input",
    "template/표준취업규칙-2026": "template/work-rules-2026",
    "template/표준취업규칙-단시간-2026": "template/work-rules-parttime-2026",
    "template/직장내괴롭힘-표준안-2026": "template/harassment-policy-2026",
    "template/표준근로계약서": "template/employment-contract-form",
}
_SLUG_TO_KEY = {v: k for k, v in _RESOURCE_SLUGS.items()}


def _register_resources():
    for i, (key, (fname, desc)) in enumerate(_RESOURCES.items()):
        def _make(f=fname):
            def _reader() -> str:
                return _read_resource(f)
            return _reader
        fn = _make()
        fn.__name__ = _RESOURCE_SLUGS[key].replace("/", "_").replace("-", "_")
        fn.__doc__ = desc
        mcp.resource(f"labor://{_RESOURCE_SLUGS[key]}", name=key,
                     description=desc, mime_type="text/markdown")(fn)


_register_resources()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
@_guard()
def labor_resource(name: str = "", chapter: str = "", article: str = "",
                   max_chars: int = 12000, start_char: int = 0) -> dict:
    """노무 지식 리소스 열람 — 체크리스트·표준 서식·판정표·입력 스키마.

    name을 비우면 사용 가능한 리소스 목록을 반환한다. 근로계약서/취업규칙 검토를
    시작할 때, 임금대장을 변환할 때, 임금항목 분류가 애매할 때 먼저 이 도구로
    해당 문서를 읽을 것.

    Args:
        name: 리소스 키 — "checklist/근로계약서", "checklist/취업규칙",
            "table/임금항목-산입매트릭스", "table/5인미만-적용제외",
            "table/시행중-개정법-기준선", "table/최저임금-연도별",
            "schema/임금대장-입력", "template/표준취업규칙-2026",
            "template/표준취업규칙-단시간-2026", "template/직장내괴롭힘-표준안-2026",
            "template/표준근로계약서"
        chapter: 특정 장만 — "제3장" 또는 "3" (표준취업규칙처럼 긴 문서용)
        article: 특정 조만 — "제17조" 또는 "17"
        max_chars: 본문 최대 길이(기본 12000). 잘리면 "잘림" 안내가 붙는다
        start_char: 이어서 읽을 시작 위치

    Note:
        표준취업규칙(약 6만 자)처럼 큰 문서는 chapter/article로 좁혀 읽을 것 —
        전체를 그대로 불러오면 컨텍스트를 크게 소모한다.
    """
    if not name:
        return {"리소스목록": [{"name": k, "슬러그": _RESOURCE_SLUGS[k], "설명": d}
                          for k, (_, d) in _RESOURCES.items()],
                "안내": "name에 위 name(한글 키) 또는 슬러그를 넣어 다시 호출하세요. "
                      "MCP 리소스 URI는 labor://<슬러그> 입니다."}
    key = name.strip()
    key = _SLUG_TO_KEY.get(key, key)
    hit = _RESOURCES.get(key)
    if not hit:
        return {"오류": f"리소스 없음: {name}",
                "리소스목록": list(_RESOURCES.keys())}
    text = _read_resource(hit[0])
    전체길이 = len(text)
    범위 = "전체"
    if chapter or article:
        text, 범위 = _slice_doc(text, chapter, article)
        if text is None:
            return {"오류": f"{범위}를 문서에서 찾지 못했습니다: {key}",
                    "안내": "chapter/article 없이 호출해 목차를 먼저 확인하세요."}
    body = text[start_char:start_char + max_chars]
    out = {"name": key, "슬러그": _RESOURCE_SLUGS[key], "범위": 범위,
           "전체길이": 전체길이, "내용": body}
    if start_char + len(body) < len(text):
        out["잘림"] = (f"{범위} {len(text)}자 중 {start_char}~{start_char + len(body)}자만 "
                     f"표시했습니다 — start_char={start_char + len(body)}로 이어 읽거나 "
                     "chapter/article로 좁혀 조회하세요.")
    return out




# ---------------------------------------------------------------------------
# 인용 검증 · 자료원 상태
# ---------------------------------------------------------------------------

# 노동위원회 사건부호 — 이게 붙으면 법원 판례가 아니라 노동위 판정례로 라우팅한다
_NLRC_MARKS = ("부해", "부노", "차별", "단협", "조정", "중재", "교섭", "복수노조", "관할")
_CITE_MAX = 10


def _norm_cite(x: str) -> str:
    """문서번호 비교용 정규화 — 공백 제거, 하이픈류 통일."""
    x = re.sub(r"\s+", "", str(x or ""))
    return x.replace("‐", "-").replace("–", "-").replace("—", "-").replace("−", "-")


def _cite_match(query: str, candidate: str) -> bool:
    """정확일치 또는 **접미 일치**.

    실무 축약은 앞쪽 기관명을 생략하는 형태다("기획재정부 근로기준정책과-73" →
    "근로기준정책과-73"). 접미로 한정하면 '2024'처럼 연도만 있는 입력이 아무 사건에나
    걸리는 오탐과, '-73'이 '-732'에 걸리는 숫자 경계 오탐이 동시에 막힌다.
    """
    q, c = _norm_cite(query), _norm_cite(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    if not c.endswith(q):
        return False
    prev = c[: -len(q)][-1:]
    return not (prev.isdigit() or q[:1].isdigit() and prev.isdigit())


def _cite_kind(cite: str) -> str:
    c = _norm_cite(cite)
    if re.fullmatch(r"\d{2}-\d{3,4}", c):
        return "법령해석례"
    if re.match(r"^[가-힣]", c) and "-" in c:
        return "행정해석"
    if re.search(r"\d", c) and any(m in c for m in _NLRC_MARKS):
        return "노동위원회"
    if re.search(r"^\D{0,4}\d{2,4}[가-힣]{1,4}\d+$", c):
        return "판례"
    if "-" in c:
        return "행정해석"
    return "미상"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def verify_citations(citations: list) -> dict:
    """인용한 노무 문서번호가 실제로 존재하는지 일괄 검증 (호출당 최대 10건).

    답변 초안에 쓴 판례·행정해석·판정례 번호를 넣으면 원천에서 조회해 실존 여부를
    확인한다. 표기는 자동 라우팅된다 — "2020다247190"(법원 판례), "근로기준정책과-3084"
    (고용노동부 행정해석), "07-0039"(법제처 법령해석례), "중앙2024부해1234"(노동위 판정례).

    판정 3값:
      확인     — 원천에서 같은 문서번호를 찾았다
      미확인   — 조회는 성공했으나 일치하는 문서가 없다 (유사 후보를 함께 제시)
      판단불가 — 원천 장애·인증 실패로 확인 자체를 못 했다 (**부존재로 단정 금지**)

    Args:
        citations: 문서번호 문자열 목록
    """
    if not isinstance(citations, list) or not citations:
        raise ValueError("citations는 문서번호 문자열의 목록이어야 합니다.")
    if len(citations) > _CITE_MAX:
        raise ValueError(f"한 번에 최대 {_CITE_MAX}건까지 검증합니다 (요청 {len(citations)}건).")

    결과 = []
    for raw in citations:
        cite = str(raw).strip()
        kind = _cite_kind(cite)
        row = {"인용": cite, "종류": kind}
        try:
            if kind == "판례":
                hits = _law.search_cases(cite, display=20)["cases"]
                키, 라벨 = "사건번호", "사건명"
            elif kind == "행정해석":
                hits = _moel.search(cite, display=20)["items"]
                키, 라벨 = "안건번호", "안건명"
            elif kind == "법령해석례":
                hits = _law.search_interpretations(cite, display=20)
                키, 라벨 = "안건번호", "안건명"
            elif kind == "노동위원회":
                hits = _nlrc.search(cite, categories=None, use_cache=True)["items"]
                키, 라벨 = "사건번호", "사건명"
            else:
                row.update({"판정": "미확인",
                            "사유": "문서번호 형식을 인식하지 못해 어느 자료원에서 찾을지 "
                                  "결정할 수 없습니다."})
                결과.append(row)
                continue
            일치 = [h for h in hits if _cite_match(cite, h.get(키, ""))]
            if 일치:
                h = 일치[0]
                row.update({"판정": "확인", "문서번호": h.get(키, ""), "제목": h.get(라벨, "")})
                for f in ("선고일자", "해석일자", "회신일자", "판정일", "법원명", "해석기관", "위원회", "출처"):
                    if h.get(f):
                        row[f] = h[f]
            else:
                row.update({"판정": "미확인",
                            "유사후보": [{"문서번호": h.get(키, ""), "제목": h.get(라벨, "")[:60]}
                                     for h in hits[:3]]})
        except Exception as e:                                  # noqa: BLE001
            row.update({"판정": "판단불가", "사유": f"{type(e).__name__}: {e}",
                        "status": _status_for(e)})
        결과.append(row)

    미확인 = sum(1 for r in 결과 if r["판정"] == "미확인")
    판단불가 = sum(1 for r in 결과 if r["판정"] == "판단불가")
    out = {"검증결과": 결과,
           "요약": {"확인": sum(1 for r in 결과 if r["판정"] == "확인"),
                  "미확인": 미확인, "판단불가": 판단불가},
           "한계": ["문서번호가 실존한다는 것이지, 그 문서가 인용한 논거를 뒷받침한다는 뜻은 "
                  "아닙니다 — 내용은 별도로 확인하세요.",
                  "'미확인'은 이 자료원에서 못 찾았다는 뜻이며, 표기 방식이 다르거나 다른 "
                  "기관 자료일 수 있습니다."]}
    if 판단불가:
        out["안내"] = (f"{판단불가}건은 원천 장애로 확인하지 못했습니다 — "
                     "**존재하지 않는다고 단정하지 마세요.**")
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@_guard()
def check_sources_health() -> dict:
    """자료원 3곳(law.go.kr · 고용노동부 행정해석 · 노동위원회)의 응답 상태 점검.

    검색 결과가 이상할 때 "내 서버 문제인지, 원천 사이트 문제인지"를 먼저 가른다.
    각 자료원에 알려진 검색어로 1회 조회해 결과 건수와 소요 시간을 보고한다.
    """
    import time as _t
    probes = [
        ("law.go.kr 판례", lambda: len(_law.search_cases("통상임금", display=1)["cases"])),
        ("법제처 법령해석례", lambda: len(_law.search_interpretations("연차", display=1))),
        ("고용노동부 행정해석", lambda: _moel.search("연차", display=1)["total"]),
        ("노동위원회 판정례", lambda: _nlrc.search("해고", categories=["부당해고"],
                                            use_cache=False)["total"] or 0),
    ]
    상태, 정상 = [], 0
    for 이름, fn in probes:
        t0 = _t.time()
        try:
            n = fn()
            정상 += 1
            상태.append({"자료원": 이름, "status": "OK", "표본건수": n,
                       "소요초": round(_t.time() - t0, 2)})
        except Exception as e:                                  # noqa: BLE001
            상태.append({"자료원": 이름, "status": _status_for(e),
                       "오류": f"{type(e).__name__}: {e}",
                       "소요초": round(_t.time() - t0, 2)})
    out = {"자료원상태": 상태, "정상": 정상, "전체": len(probes)}
    if 정상 < len(probes):
        out["안내"] = ("일부 자료원이 응답하지 않습니다 — 그 자료원의 검색 결과가 "
                     "0건이어도 자료 부존재로 판단하지 마세요. "
                     "AUTH_ERROR면 서버의 law.go.kr 기관코드·IP 등록 확인이 필요합니다.")
    return out


# ---------------------------------------------------------------------------
# 검토 프롬프트 2종
# ---------------------------------------------------------------------------

@mcp.prompt()
def review_employment_contract() -> str:
    """근로계약서 검토 절차 — 체크리스트 기반 5단계 검토를 시작합니다."""
    return (
        "첨부된 근로계약서를 다음 절차로 검토해 주세요.\n\n"
        "1단계 — 문서 통독: 계약서 전문을 읽고 근로자 유형(정규/기간제/단시간/연소자/"
        "외국인/일용/감시단속)과 사업장 규모(5인 이상 여부)를 먼저 파악.\n"
        "2단계 — 체크리스트 대조: labor_resource(\"checklist/근로계약서\")를 로드해 "
        "필수기재사항(근기법 17조, 기간제법 17조)과 위법조항 15유형을 항목별로 대조. "
        "5인 미만 사업장이면 labor_resource(\"table/5인미만-적용제외\")를 함께 확인.\n"
        "3단계 — 근거 조회: 의심 조항마다 labor_law_article로 현행 조문을 확인하고, "
        "해석이 갈리는 쟁점은 moel_interpretation_search(고용노동부 행정해석)와 "
        "labor_case_search(판례)로 근거를 확보.\n"
        "4단계 — 수치 검증: 임금 조항은 check_minimum_wage(산입 분해)와 "
        "calc_ordinary_avg_wage로, 포괄임금이면 고정OT를 역산해 미달 여부 확인. "
        "주휴·연차·퇴직급여·해고예고 조항도 해당 계산 도구로 검증.\n"
        "5단계 — 결과 정리: 위반·위험 항목별로 {① 해당 조항 인용, ② 위반 근거"
        "(조문·판례·행정해석 번호), ③ 무효 시 대체되는 법정 기준(근기법 15조), "
        "④ 수정 문안 제안} 형식으로 출력하고, 마지막에 적법 항목 요약과 "
        "\"최종 판단은 공인노무사 확인 필요\" 문구를 포함.\n\n"
        "작성 요청이면: labor_resource(\"template/표준근로계약서\")의 해당 서식을 "
        "기초로 사업장 조건을 반영해 작성한 뒤, 같은 체크리스트로 자체 검증하세요."
    )


@mcp.prompt()
def review_work_rules() -> str:
    """취업규칙 검토 절차 — 필수기재·법 위반·불이익변경 3축 검토를 시작합니다."""
    return (
        "첨부된 취업규칙을 다음 절차로 검토해 주세요.\n\n"
        "1단계 — 문서 통독: 장·조문 구조를 파악하고 제정/최근 개정 시점, 사업장 "
        "규모(10인 이상 신고 의무), 과반수 노조 유무를 확인.\n"
        "2단계 — 필수기재 검사: labor_resource(\"checklist/취업규칙\")를 로드해 "
        "근기법 93조 14개 항목의 존재 여부를 매핑 (누락 빈발: 9의2호 환경개선, "
        "8호 모성보호 최신 개정 반영, 11호 직장 내 괴롭힘).\n"
        "3단계 — 법 위반 검사: 연차·감급 한도(calc_wage_cut_limit)·징계 절차·"
        "근로시간 조항이 현행법 미달인지 labor_law_article로 조문 대조. "
        "labor_resource(\"table/시행중-개정법-기준선\")로 최신 개정(육아지원 3법, "
        "임금체불 제재 등) 반영 여부 확인.\n"
        "4단계 — 표준안 대조: labor_resource(\"template/표준취업규칙-2026\")과 "
        "장·조문 단위로 대비해 누락 장(괴롭힘·성희롱 예방 등)과 구법 기준 조문을 발견.\n"
        "5단계 — 불이익변경 검토(변경안인 경우): 기존 규칙과 문언 비교로 불이익 여부 "
        "판단 후, 동의 절차가 2023 전합(2017다35588)·2026 판례(2025다215010 — "
        "전산망 개별 동의만으로는 무효) 기준을 충족하는지 검토.\n"
        "6단계 — 결과 정리: {필수기재 누락 / 법 위반(무효+대체 기준) / 개정 권고 / "
        "신고 절차 안내(시행규칙 15조 — 의견청취서·동의서·신구대비표)} 순으로 출력하고 "
        "\"최종 판단은 공인노무사 확인 필요\" 문구를 포함.\n\n"
        "작성 요청이면: 표준취업규칙을 기초로 사업장 조건([필수] 전부 + 해당되는 "
        "[선택])을 반영해 작성한 뒤, 같은 체크리스트로 자체 검증하세요."
    )


if __name__ == "__main__":
    import asyncio as _aio
    _n_tools = len(_aio.run(mcp.list_tools()))
    logging.info("labor-mcp 서버 시작 — %s:%s, 도구 %d종, 리소스 %d종",
                 mcp.settings.host, PORT, _n_tools, len(_RESOURCES))
    mcp.run(transport="streamable-http")
