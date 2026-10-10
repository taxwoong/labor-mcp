# -*- coding: utf-8 -*-
"""
law_go_kr.py — 국가법령정보센터 Open API(DRF) 클라이언트
nts-tax-mcp 확장 모듈: 대법원 판례 · 법령(현행+연혁) · 법령해석례

- 인증: 환경변수 LAW_API_OC(law.go.kr 가입 시 발급받는 기관코드) 필수. 등록된 IP에서만 동작.
- 응답: XML을 경량 파싱 (JSON 지원이 target마다 들쭉날쭉해 XML로 통일).
- 연혁 워크플로우(2026-07-21 확립, 2026-10-09 수정): lawSearch target=eflaw로 시행본 목록
  → 특정 시행본(연혁 행 = MST + 시행일자)의 문안은 lawService target=eflaw&MST=…&efYd=<행 시행일자>
  (법제처 시행일 기준 통합본) → 조문은 <조문번호> 위치 기준 순차 슬라이스.
  통합본 조회가 실패하면 target=law&MST=…(그 MST 공포 당시 원문)로 폴백하고 '주의'로 알린다.
"""
import logging
import os
import re
import html
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

BASE = "http://www.law.go.kr/DRF"
OC = os.environ.get("LAW_API_OC", "")
TIMEOUT = 20

# 같은 법령의 조문을 연속 조회하면 매번 법 전체 XML(소득세법 61만 자 등)을
# 다시 받게 되므로, 응답을 짧게 캐싱한다. 법령 개정은 실시간이 아니므로 안전.
_CACHE_TTL = 600   # 초
_CACHE_MAX = 16    # 전문 XML이 커서(수십만 자) 항목 수로 메모리 상한
_cache = {}        # key -> (만료시각, 응답텍스트)

_TAG_RE = re.compile(r"<([^/>\s]+)>\s*(.*?)\s*</\1>", re.S)

# 기관코드(OC) 가림 — law.go.kr 요청 URL에 OC가 쿼리로 붙어, requests 예외 문자열('… for url: …?OC=…')이
# LawUpstreamError 메시지에 그대로 실려 도구 응답 '오류' 필드·서버 로그·아카이브 적재(ingest) 로그로 나갔다
# (2026-10-09 리뷰 재현: 가짜 OC와 닫힌 포트로 law_article_as_of → LawUpstreamError 메시지에 'OC=<값>')
_OC_PARAM_RE = re.compile(r"(OC=)[^&\s'\"<>]+")


def mask_oc(text) -> str:
    """문자열(또는 예외)에서 기관코드를 가린다 — 'OC=<값>' → 'OC=***', 값 자체가 나와도 '***'."""
    s = str(text)
    if OC:
        s = s.replace(OC, "***")
    return _OC_PARAM_RE.sub(r"\1***", s)


def _masked_request_error(e):
    """requests 예외를 같은 종류의 새 예외로 바꿔 메시지의 OC를 가린다. 응답 객체는 그대로 둔다
    (_version_xml이 e.__cause__의 HTTP 상태코드로 폴백 여부를 정한다)."""
    msg = mask_oc(e)
    try:
        return type(e)(msg, response=getattr(e, "response", None))
    except Exception:  # noqa: BLE001 — 생성자 인자가 다른 하위 예외
        return requests.exceptions.RequestException(msg, response=getattr(e, "response", None))


class OcMaskFilter(logging.Filter):
    """로그 레코드의 메시지·트레이스백에서 기관코드를 가린다 (핸들러에 단다)."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — 형식 오류 레코드는 그대로 둔다
            return True
        record.msg, record.args = mask_oc(msg), ()
        if record.exc_info:
            record.exc_text = mask_oc(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_oc_log_mask(logger=None):
    """로거(기본: 루트)의 핸들러마다 OcMaskFilter를 단다. logging 설정 뒤에 부른다 (server.py·ingest_archive.py)."""
    lg = logger or logging.getLogger()
    for h in lg.handlers:
        if not any(isinstance(f, OcMaskFilter) for f in h.filters):
            h.addFilter(OcMaskFilter())

# 시행본 문안의 원천 — target=law(공포 당시 원문)에서 target=eflaw(시행일 기준 통합본)로 바꿨다.
# target=law&MST=…는 그 MST가 '공포될 당시'의 원문이다. 연혁 목록에서 같은 MST가 더 늦은 시행일자
# 행으로 다시 나오면(조문별 단계 시행, 타법개정의 늦은 시행일) 그 MST를 현행으로 고르게 되고,
# 그 MST보다 늦게 공포됐지만 이미 시행된 개정이 통째로 빠진다. 거꾸로 아직 시행 전인 문안은 미리
# 들어 있다 (2026-10-09 실측: 근로기준법 현행 행 MST 285279(2026.4.7 공포, 시행 20261008) 원문에는
# 2026.8.4 공포·2026.10.2 시행 타법개정이 없고, 2026.12.8 시행 예정인 제101조 '노동감독관' 문안과
# 2027.1.1 신설 제44조의4가 들어 있었다. 국민연금법 현행 행 MST 280269(2025.12.16 공포, 시행
# 20260617) 원문에는 2026.5.26 개정이 없었다).
# target=eflaw&MST=…&efYd=<그 행의 시행일자>는 법제처가 그 시행일 기준으로 만든 통합본이다
# (2026-10-09 실측, 노동법·세법 7개 법령의 1949~2027년 시행본 70개, 통합본 조회 실패 0건):
#   - 단계 시행을 날짜대로 반영한다 (근로기준법 제101조는 20261208 통합본부터 개정 문안)
#   - 같은 시행일자 행은 MST가 달라도 조문 문안이 같다 (소득세법 20260101 행 MST 4개 문안 지문 동일)
#   - 부칙은 그날까지 시행된 개정의 것만 있다 (공포됐지만 아직 시행 전인 개정의 부칙은 없음)
#   - efYd가 그 MST의 연혁 행 시행일자가 아니면 HTTP 200에 '일치하는 법령이 없습니다'를 준다
#     (efYd 없이 부르면 HTML 오류 화면, ID로 부르고 날짜가 어긋나면 HTTP 500)
_NO_MATCH_TEXT = "일치하는 법령이 없습니다"
# 조문 일부(항·호)만 늦게 시행되면 통합본은 그 문안을 미리 싣고 <조문참고자료>에 시행일을 표시한다.
# 표시 형식은 두 가지다 (2026-10-09 실측):
#   ① "[시행일: 2027.1.1] 제116조제1항제2호" (근로기준법 20261008 통합본 제116조)
#   ② "[시행일] 제55조제2항의 개정규정은 다음 각 호의 구분에 따른 날부터 시행한다.
#        1. 상시 300명 이상의 근로자를 사용하는 사업 또는 사업장 …: 2020년 1월 1일 …" — 사업장 규모별 시행일
#      (근로기준법 제55조 20200526 통합본, 산업안전보건법 제175조 20270101 통합본 — 예전 정규식은 ①만 잡아
#      50명 미만 사업장에 아직 시행 전인 위험성평가 과태료 문안이 경고 없이 나갔다)
_STAGED_HEAD_RE = re.compile(r"\[시행일\s*(?::\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?)?\s*\]")
_STAGED_ITEM_RE = re.compile(r"(?m)^\s*\d+\.\s*(.*)\s*[:：]\s*(\d{4})\s*(?:년|\.)\s*(\d{1,2})\s*(?:월|\.)\s*"
                             r"(\d{1,2})\s*일?\.?\s*$")
_ANY_DATE_RE = re.compile(r"(\d{4})\s*(?:년|\.)\s*(\d{1,2})\s*(?:월|\.)\s*(\d{1,2})")
# 조문 문안 속 개정 이력 주석 — CDATA 안에 '<개정 2015.5.13, 2026.4.21>', '<신설 …>', '삭제 <2017.12.19>'처럼
# 꺾쇠째 들어 있어 태그 제거(_strip_tags)에 함께 지워진다. 통합본에는 공포본의 <항제개정일자문자열> 태그가
# 없어 이 주석이 개정 이력의 유일한 표시다 (2026-10-09 실측) — 태그로 오인되지 않게 〈…〉로 바꿔 보존한다
_NOTE_KEEP_RE = re.compile(
    r"<((?:개정|신설|삭제|전문개정|본조신설|본조삭제|제목개정|제목신설|타법개정)(?=[\s\d>,])[^<>]*"
    r"|\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?(?:\s*,\s*\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?)*)>")
# 조문참고자료의 대괄호 개정 주석 — '[본조신설 2019.12.31]', '[전문개정 2025.12.23]', '[종전 … 〈2010.12.27〉]'
_BRACKET_NOTE_RE = re.compile(r"\[((?:본조신설|전문개정|제목개정|제목신설|본조삭제|종전|조번호개정)[^\[\]]*)\]")
_NOTE_DATE_RE = re.compile(r"(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})")
_CONTENT_TAG_RE = re.compile(r"<(조문내용|항내용|호내용|목내용)>(.*?)</\1>", re.S)
_MIDDOT_RE = re.compile(r"[\s·ㆍ‧・]")
_BOX_RE = re.compile(r"[─-╿]")   # 표 괘선 문자
_KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    """오늘 날짜(한국 시간, YYYYMMDD) — '현행'·'과거 기준일'·'아직 시행 전' 판정의 기준."""
    return datetime.now(_KST).strftime("%Y%m%d")


def _keep_notes(text: str) -> str:
    """CDATA를 벗긴 문안에서 개정 주석 꺾쇠 '<…>'를 '〈…〉'로 바꾼다 (태그 제거 전에 호출)."""
    return _NOTE_KEEP_RE.sub(r"〈\1〉", text)


def _norm_name(name: str) -> str:
    """법령명 비교용 — 공백과 가운뎃점(·ㆍ‧・)을 뺀다 ('노동조합및노동관계조정법' = '노동조합 및 노동관계조정법')."""
    return _MIDDOT_RE.sub("", name or "")


def _norm_article_no(article_no) -> str:
    """조번호 입력을 '17' 또는 '76의2' 형식으로 정규화한다. '제17조'·'17조'·'76의 2'·'제76조의2'·'013'을 받는다.
    형식이 아니면 LawInvalidInput — 예전에는 형식 오류가 NOT_FOUND('자료 부존재로 판단해도 됨')로 나가
    '근로기준법 제17조는 없다'로 읽혔다 (2026-10-09 실측: '제17조'·'17조'·'76의 2' → NOT_FOUND '제제17조조')."""
    t = re.sub(r"\s+", "", str(article_no or ""))
    m = re.fullmatch(r"제?0*(\d+)조?(?:의0*(\d+))?조?", t)
    if not m:
        raise LawInvalidInput(f"article_no 형식 오류: '{article_no}' — 조번호만 넣으세요 "
                              f"(예: '17', '76의2', '제76조의2'. 항·호는 빼고 넣음)")
    return str(int(m.group(1))) + (f"의{int(m.group(2))}" if m.group(2) else "")


def _article_label(article_no: str) -> str:
    m = re.match(r"^(\d+)(?:의(\d+))?$", article_no)
    return (f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")) if m else f"제{article_no}조"


def _article_pat(label: str):
    """'제96조'가 '제96조의2'·'제960조'에, '제59조의2'가 '제59조의21'에 걸리지 않는 정규식.
    '제30조의 개정규정'처럼 소유격 '의'는 허용한다 — 예전 (?!의) 가드는 이 흔한 표현까지 걸러 부칙 언급을
    놓쳤다 (2026-10-09 실측: 노인장기요양보험법 제21690호 부칙 '제30조의 개정규정은 공포한 날부터 시행한다')."""
    return re.compile(re.escape(label) + (r"(?!\d)" if "의" in label else r"(?!의\d|\d)"))


def _staged_notes(text: str):
    """조문 텍스트의 [시행일] 표시를 모두 뽑는다 (기준일 비교 전). 반환 [{"시행일자", "대상"(, "조건")}] —
    형식 ②(사업장 규모별)는 각 호마다 하나씩 '조건'을 붙인다."""
    out = []
    heads = list(_STAGED_HEAD_RE.finditer(text))
    for i, h in enumerate(heads):
        seg = text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        nb = re.search(r"\n\s*\[", seg)   # 뒤이은 '[본조신설 …]' 같은 대괄호 주석 줄에서 끊는다
        if nb:
            seg = seg[:nb.start()]
        lines = [ln.strip() for ln in seg.strip().splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        if h.group(1):
            out.append({"시행일자": f"{h.group(1)}{int(h.group(2)):02d}{int(h.group(3)):02d}",
                        "대상": first.rstrip(",").strip()})
            continue
        what = re.split(r"(?:의\s*개정규정)?\s*(?:은|는)?\s*다음\s*각\s*호의", first)[0].strip() or first
        items = _STAGED_ITEM_RE.findall(seg)
        for cond, y, mo, d in items:
            out.append({"시행일자": f"{y}{int(mo):02d}{int(d):02d}", "대상": what, "조건": cond.strip()})
        if not items:   # 각 호가 없는 다른 형식 — 날짜만이라도 뽑는다
            for y, mo, d in _ANY_DATE_RE.findall(seg):
                out.append({"시행일자": f"{y}{int(mo):02d}{int(d):02d}", "대상": what})
    return out


def _staged_caution(staged) -> str:
    """시행예정_부분 → '주의' 문구."""
    parts = []
    for s in staged:
        cond = f" [{s['조건'][:60]}{'…' if len(s['조건']) > 60 else ''}]" if s.get("조건") else ""
        parts.append(f"{s['대상']}{cond}: {s['시행일자']} 시행")
    msg = ("이 조문 일부(" + ", ".join(parts) + ")는 아직 시행 전인데 법제처 통합본에 [시행일] 표시와 함께 "
           "미리 실려 있음 — 그 부분은 표시된 시행일부터 적용")
    if any(s.get("조건") for s in staged):
        msg += " (사업장 규모 등 조건별로 시행일이 달라 해당 사업장에는 아직 시행 전일 수 있음)"
    return msg


def _norm_key(lines):
    """문안 같음 판정용 키 — 표 괘선 폭·전각/반각 기호·공백·줄바꿈 차이를 무시한다."""
    if lines is None:
        return None
    t = unicodedata.normalize("NFKC", "\n".join(lines))
    t = _BOX_RE.sub("", t).replace("?", "-")
    return re.sub(r"\s+", "", t)


def _add_caution(out: dict, msg: str):
    """응답의 '주의'에 문구를 덧붙인다 (이미 있으면 ' / '로 잇는다)."""
    if msg:
        out["주의"] = f"{out['주의']} / {msg}" if out.get("주의") else msg


class LawGoKrError(Exception):
    """law.go.kr 클라이언트 공통 예외 (하위 예외의 기반 — 기존 except 절 호환)."""


class LawAuthError(LawGoKrError):
    """OC 미설정·오류, IP 미등록, 호출한도 초과 — 자료 부존재와 무관."""


class LawUpstreamError(LawGoKrError):
    """네트워크·HTTP 오류 — 자료 부존재와 무관."""


class LawNotFound(LawGoKrError):
    """검색·조회는 성공했으나 해당 자료가 없음."""


class LawInvalidInput(LawGoKrError, ValueError):
    """입력 형식 오류. ValueError를 겸해 기존 except ValueError 경로와도 호환."""


# MCP 응답의 status 5값 — 원천 장애(UPSTREAM/PARSE/AUTH)를 "자료 없음"(NOT_FOUND)과
# 절대 섞지 않는 것이 이 계약의 목적이다.
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_UPSTREAM = "UPSTREAM_ERROR"
STATUS_PARSE = "PARSE_ERROR"
STATUS_AUTH = "AUTH_ERROR"
STATUS_INVALID = "INVALID_INPUT"


def status_of(exc: BaseException) -> str:
    """예외를 status 값으로 매핑 (law.go.kr 계열 + requests)."""
    if isinstance(exc, LawAuthError):
        return STATUS_AUTH
    if isinstance(exc, LawNotFound):
        return STATUS_NOT_FOUND
    if isinstance(exc, LawInvalidInput):
        return STATUS_INVALID
    if isinstance(exc, (LawUpstreamError, requests.exceptions.RequestException)):
        return STATUS_UPSTREAM
    if isinstance(exc, ValueError):
        return STATUS_INVALID
    return STATUS_UPSTREAM


_DATE_SEP_RE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")


def _norm_date(value: str, field: str = "날짜") -> str:
    """YYYYMMDD로 정규화. 'YYYY-MM-DD'·'YYYY.MM.DD'는 받아주고, 그 외 형식은 거부한다.

    문자열 비교로 시행본을 고르는 코드가 많아, 형식이 어긋난 값을 그대로 흘려보내면
    오류 없이 **다른 시점의 조문**이 선택된다(2026-09-01 리뷰에서 실측 확인).
    """
    if value in (None, ""):
        return ""
    v = str(value).strip()
    if re.fullmatch(r"\d{8}", v):
        pass
    else:
        m = _DATE_SEP_RE.match(v)
        if not m:
            raise LawInvalidInput(
                f"{field} 형식 오류: {value!r} — YYYYMMDD 8자리로 넣으세요 (예: 20250315). "
                "형식이 맞지 않으면 다른 시점의 조문이 조용히 선택될 수 있어 거부합니다.")
        v = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    try:
        time.strptime(v, "%Y%m%d")
    except ValueError:
        raise LawInvalidInput(f"{field} 형식 오류: {value!r} — 존재하지 않는 날짜입니다.")
    return v


def _strip_cdata(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    return html.unescape(s).strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# 인증 실패 응답은 HTTP 200 + 아래 봉투로 온다 (2026-09-01 실측):
#   <Response><result>사용자 정보 검증에 실패하였습니다.</result>
#            <msg>… 정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요.</msg></Response>
# 본문에 "인증"이라는 단어가 없어서, 예전의 ("인증" and "실패") 조건은 전혀 걸리지 않았고
# 이 오류 XML이 정상 응답으로 파싱돼 모든 도구가 조용히 "0건"을 반환했다(캐싱까지 됐다).
_AUTH_FAIL_RE = re.compile(r"<result>\s*(?:<!\[CDATA\[)?\s*([^<\]]*?)\s*(?:\]\]>)?\s*</result>", re.S)
_AUTH_FAIL_HINTS = ("검증에 실패", "인증", "등록되지 않은", "권한이 없", "횟수를 초과", "일일 요청")

# law.go.kr이 인식하지 못하는 court 분류어 — 넘기면 오류 없이 0건이 되어 "판례 없음"으로 둔갑한다
_BAD_COURT_VALUES = {"하위법원", "하급심", "하급법원", "지방법원급", "1심", "2심", "supreme", "lower"}

# 행정규칙(admrul) 검색 필터 — org는 law.go.kr 소관부처 코드, knd는 가이드 문서의 종류 코드
ADMRUL_ORG_CODES = {
    "고용노동부": "1492000",
}
ADMRUL_KIND_CODES = {"훈령": "1", "예규": "2", "고시": "3", "공고": "4", "지침": "5", "기타": "6"}

# 원문 확인용 공개 URL. law.go.kr XML의 '...상세링크'는 `?OC=<기관코드>`가 박힌 DRF
# 주소라서 그대로 노출하면 **API 자격증명이 대화 컨텍스트로 새어나간다** — 쓰지 않는다.
# 아래 두 패턴은 OC 없이 열리는 것을 실측 확인했다 (2026-09-01).
PREC_VIEWER = "https://www.law.go.kr/precInfoP.do?precSeq={}"
EXPC_VIEWER = "https://www.law.go.kr/expcInfoP.do?expcSeq={}"


def _check_auth_error(text: str) -> None:
    """법제처 오류 봉투를 감지해 AuthError로 승격. 감지되면 캐싱하지 않는다."""
    head = text[:1000]
    if "<Response>" not in head and "<response>" not in head:
        return
    m = _AUTH_FAIL_RE.search(head)
    if not m:
        return
    msg = m.group(1).strip()
    if not msg or not any(h in msg for h in _AUTH_FAIL_HINTS):
        return
    detail = ""
    m2 = re.search(r"<msg>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</msg>", head, re.S)
    if m2:
        detail = " / " + _strip_cdata(m2.group(1))
    raise LawAuthError(
        f"law.go.kr 인증 실패: {msg}{detail} — OC 또는 서버 IP 등록을 확인하세요 "
        "(open.law.go.kr에서 현재 서버 공인 IP 등록). "
        "이것은 자료가 없다는 뜻이 아닙니다 — 검색 결과 0건으로 해석하지 마세요.")


def _get(endpoint: str, **params) -> str:
    if not OC:
        raise LawAuthError(
            "LAW_API_OC 환경변수가 설정되지 않았습니다 — law.go.kr에서 발급받은 기관코드(OC)를 설정하세요. "
            "이것은 자료가 없다는 뜻이 아닙니다.")
    p = {"OC": OC, "type": "XML"}
    p.update({k: v for k, v in params.items() if v not in (None, "", 0)})
    key = (endpoint, tuple(sorted(p.items())))
    hit = _cache.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = requests.get(f"{BASE}/{endpoint}", params=p, timeout=TIMEOUT,
                         headers={"User-Agent": "labor-mcp/1.0"})
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        # 원인 예외도 OC를 가린 새 예외로 바꿔 단다 — 트레이스백 로그(LOG.exception)에 원래 체인이 찍히지 않게
        cause = _masked_request_error(e)
        raise LawUpstreamError(mask_oc(
            f"law.go.kr 접속 실패: {type(e).__name__}: {cause} — 원천 사이트 장애·네트워크 문제입니다. "
            "이것은 자료가 없다는 뜻이 아닙니다.")) from cause
    text = r.text
    _check_auth_error(text)   # 오류 응답은 캐싱하지 않는다 (복구 후에도 10분간 0건을 서빙하던 문제)
    if len(_cache) >= _CACHE_MAX:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[key] = (now + _CACHE_TTL, text)
    return text


def _parse_items(xml: str, item_tag: str):
    """<item_tag>…</item_tag> 블록마다 자식 태그를 dict로."""
    items = []
    for m in re.finditer(rf"<{item_tag}(?:\s[^>]*)?>(.*?)</{item_tag}>", xml, re.S):
        block = m.group(1)
        d = {}
        for tag, val in _TAG_RE.findall(block):
            d[tag] = _strip_cdata(val)
        if d:
            items.append(d)
    return items


def _same_name_only(rows: list, law_name: str) -> list:
    """법령ID 없이 이름으로 연혁을 찾으면 그 이름으로 시작하는 시행령·시행규칙 시행본까지 섞이고,
    시행일이 더 늦은 하위법령 조문이 반환된다 (2026-10-09 실측: '노인장기요양보험법' 제9조·제10조가
    시행령 조문으로 나옴). 이름이 정확히 같은 법령(공백·가운뎃점 무시)의 법령ID 행만 남긴다 — 같은 ID면
    옛 이름의 시행본도 남는다. 같은 이름이 없으면 그대로 둔다(연혁 목록용 — 조문 조회는
    _require_exact_name으로 거부).
    같은 이름의 법령ID가 여럿이어도 그 ID들로 거른다 — 폐지 후 재제정된 법은 옛 ID와 새 ID가 이름이 같다.
    예전에는 여럿이면 거르지 않아 하위법령 행이 남았다 (2026-10-09 실측: '노동조합및노동관계조정법'
    (000142·000143) 제2조가 시행규칙 제2조로 status OK)."""
    key = _norm_name(law_name)
    ids = {r["법령ID"] for r in rows if _norm_name(r.get("법령명", "")) == key}
    if not ids:
        return rows
    return [r for r in rows if r["법령ID"] in ids]


def _require_exact_name(rows: list, law_name: str):
    """법령ID 없이 이름으로 조문·부칙을 찾을 때, 이름이 정확히 같은 법령이 연혁에 없으면 거부한다(INVALID_INPUT).
    그대로 두면 이름 검색에 섞인 다른 법령의 조문이 OK로 나간다 (2026-10-09 실측: law='고용보험' 제2조가
    '고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률' 제2조로 status OK)."""
    key = _norm_name(law_name)
    if any(_norm_name(r.get("법령명", "")) == key for r in rows):
        return
    names = []
    for r in rows:
        n = r.get("법령명", "")
        if n and n not in names:
            names.append(n)
    raise LawInvalidInput(
        f"'{law_name}'과(와) 이름이 정확히 같은 법령이 연혁 검색 결과에 없음 — 정식 법령명이나 통칭"
        f"(근기법·산재보험법·고용산재보험료징수법 등)으로 다시 조회하세요"
        + (f" (검색된 법령: {', '.join(names[:10])}{' 등' if len(names) > 10 else ''})" if names else ""))


class LawGoKrClient:
    # ---------- 판례 (법제처 제공 대법원·하급심) ----------
    def search_cases(self, keyword: str, court: str = "", date_from: str = "",
                     date_to: str = "", display: int = 10, page: int = 1):
        """target=prec 판례 검색.

        court: **법원명을 그대로** 넣는다 — '대법원', '서울고등법원' 등(빈값=전체).
          '하위법원'·'하급심' 같은 분류어는 API가 받지 않아 **항상 0건**이 되므로 거부한다
          (2026-09-01 실측: 통상임금 → 하위법원 0건 / 대법원 531건 / 서울고등법원 195건).
        date_from/date_to: YYYYMMDD (선고일자 범위)."""
        date_from = _norm_date(date_from, "date_from")
        date_to = _norm_date(date_to, "date_to")
        params = dict(target="prec", query=keyword, display=display, page=page, search=2)
        if court:
            if court.strip() in _BAD_COURT_VALUES:
                raise LawInvalidInput(
                    f"court={court!r}는 law.go.kr이 인식하지 못해 항상 0건이 됩니다 — "
                    "법원명을 그대로 넣으세요 (예: '대법원', '서울고등법원'). "
                    "하급심 전체를 한 번에 거르는 필터는 제공되지 않습니다.")
            params["curt"] = court
        if date_from or date_to:
            params["prncYd"] = f"{date_from or '19450815'}~{date_to or '20991231'}"
        xml = _get("lawSearch.do", **params)
        items = _parse_items(xml, "prec")
        total = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        return {
            "total": int(total.group(1)) if total else len(items),
            "cases": [{
                "판례일련번호": it.get("판례일련번호", ""),
                "사건명": it.get("사건명", ""),
                "사건번호": it.get("사건번호", ""),
                "법원명": it.get("법원명", ""),
                "선고일자": it.get("선고일자", ""),
                "판결유형": it.get("판결유형", ""),
                "사건종류명": it.get("사건종류명", ""),
                "출처": PREC_VIEWER.format(it["판례일련번호"]) if it.get("판례일련번호") else "",
            } for it in items],
        }

    def get_case(self, case_serial: str, max_chars: int = 8000):
        """target=prec 판례 본문. case_serial = 판례일련번호."""
        xml = _get("lawService.do", target="prec", ID=case_serial)
        d, truncated = {}, []
        for tag in ("사건명", "사건번호", "법원명", "선고일자", "판시사항", "판결요지", "참조조문", "참조판례", "판례내용"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if m:
                full = _strip_tags(_strip_cdata(m.group(1)))
                limit = max_chars if tag == "판례내용" else max(max_chars, 4000)
                d[tag] = full[:limit]
                if len(full) > limit:
                    truncated.append(f"{tag}({len(full)}자 → {limit}자)")
        if not d:
            raise LawNotFound(f"판례 본문 없음 (일련번호 {case_serial}) — 응답 앞부분: {xml[:200]}")
        d["출처"] = PREC_VIEWER.format(case_serial)
        if truncated:
            # 고지가 없으면 LLM이 문장 중간에서 끊긴 판시를 전문으로 착각한다
            d["잘림"] = (f"길이 제한으로 잘렸습니다: {', '.join(truncated)}. "
                        "전문이 필요하면 max_chars를 키워 다시 호출하세요.")
        return d

    # ---------- 법령해석례 (법제처) ----------
    def search_interpretations(self, keyword: str, display: int = 10, page: int = 1):
        """target=expc 법령해석례 검색."""
        xml = _get("lawSearch.do", target="expc", query=keyword, display=display, page=page)
        items = _parse_items(xml, "expc")
        rows = []
        for it in items:
            seq = it.get("법령해석례일련번호") or it.get("일련번호", "")
            rows.append({
                "해석례일련번호": seq,
                "안건명": it.get("안건명", ""),
                "안건번호": it.get("안건번호", ""),
                "회신기관": it.get("회신기관명", it.get("질의기관명", "")),
                "회신일자": it.get("회신일자", ""),
                "출처": EXPC_VIEWER.format(seq) if seq else "",
            })
        return rows

    def get_interpretation(self, serial: str, max_chars: int = 8000):
        xml = _get("lawService.do", target="expc", ID=serial)
        d = {}
        for tag in ("안건명", "안건번호", "회신일자", "질의요지", "회답", "이유"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if m:
                d[tag] = _strip_tags(_strip_cdata(m.group(1)))[:max_chars]
        return d or {"오류": "본문 없음", "응답": xml[:200]}

    # ---------- 법령: 현행 검색 ----------
    def search_laws(self, law_name: str, display: int = 20):
        """target=law 현행 법령 검색 → 법령ID·MST(법령일련번호)·시행일자."""
        xml = _get("lawSearch.do", target="law", query=law_name, display=display)
        items = _parse_items(xml, "law")
        return [{
            "법령명": it.get("법령명한글", ""),
            "법령ID": it.get("법령ID", ""),
            "MST": it.get("법령일련번호", ""),
            "시행일자": it.get("시행일자", ""),
            "공포일자": it.get("공포일자", ""),
            "공포번호": it.get("공포번호", ""),
            "제개정구분": it.get("제개정구분명", ""),
        } for it in items]

    # ---------- 법령: 연혁(시행본) 목록 ----------
    @staticmethod
    def _eflaw_row(it: dict) -> dict:
        return {
            "법령명": it.get("법령명한글", ""),
            "법령ID": it.get("법령ID", ""),
            "MST": it.get("법령일련번호", ""),
            "시행일자": it.get("시행일자", ""),
            "공포일자": it.get("공포일자", ""),
            "공포번호": it.get("공포번호", ""),
            "제개정구분": it.get("제개정구분명", ""),
        }

    def _eflaw_rows(self, max_rows: int, **query) -> list:
        """target=eflaw 검색을 100건씩 쪽 넘겨 모은다. query는 query=<법령명> 또는 LID=<법령ID>.
        한 쪽이 100건 미만이면 마지막 쪽이다 — 빈 쪽을 한 번 더 요청하지 않는다."""
        rows, page = [], 1
        while len(rows) < max_rows and page <= 10:
            # 페이지 크기 파라미터는 display가 맞음 — numOfRows는 무시되어 20건씩 5왕복하게 됨 (2026-08-18 실측)
            xml = _get("lawSearch.do", target="eflaw", display=100, page=page, **query)
            items = _parse_items(xml, "law")
            if not items:
                break
            rows.extend(self._eflaw_row(it) for it in items)
            if len(items) < 100:
                break
            page += 1
        return rows

    def _add_renamed_rows(self, rows: list, max_rows: int) -> list:
        """이름으로 찾은 연혁에 개명 전 시행본을 보탠다. 법제처는 법령ID가 같아도 이름 검색에는 그 이름 시절의
        행만 준다 — 남녀고용평등법(1988~2007)과 '남녀고용평등과 일·가정 양립 지원에 관한 법률'(2008~)은 같은
        법령ID 000130인데 이름 검색은 후자 31행만 주고 1988~2007 시행본이 빠졌다(2026-10-10 실측).
        제정 행이 없는 ID(= 앞쪽이 잘렸다)만 LID로 다시 받아 합친다 — 제정 행이 있으면 개명 없는 완결된
        연혁이라 왕복이 늘지 않는다."""
        by_id = {}
        for r in rows:
            by_id.setdefault(r.get("법령ID", ""), []).append(r)
        have = {(r["MST"], r["시행일자"]) for r in rows}
        out = list(rows)
        for lid, rs in by_id.items():
            if not lid or any((r.get("제개정구분") or "") == "제정" for r in rs):
                continue
            for r in self._eflaw_rows(max_rows, LID=lid):
                if r.get("법령ID") == lid and (r["MST"], r["시행일자"]) not in have:
                    have.add((r["MST"], r["시행일자"]))
                    out.append(r)
        return out

    def law_history(self, law_name: str, law_id: str = "", max_rows: int = 500):
        """target=eflaw 연혁 시행본 전체 목록. law_id를 주면 그 본법만 필터
        (예: 부가가치세법=001571 — 같은 이름 하위법령 혼입 방지).
        개명된 법령은 개명 전 시행본까지 포함한다(법령ID가 같으면 같은 법)."""
        rows = []
        if law_id:
            # 법령ID를 알면 이름 검색을 거치지 않고 ID로 바로 받는다 — 이름 검색은 그 이름 시절의 행만 줘서
            # 개명 전 시행본이 빠진다
            rows = [r for r in self._eflaw_rows(max_rows, LID=law_id) if r.get("법령ID") == law_id]
        if not rows:   # 법령ID가 없거나, ID 검색이 비면(형식 차이 등) 이름 검색으로 되돌아간다
            rows = self._eflaw_rows(max_rows, query=law_name)
            if law_id:
                rows = [r for r in rows if r.get("법령ID") == law_id]
            else:
                rows = _same_name_only(rows, law_name)
            if rows:
                rows = self._add_renamed_rows(rows, max_rows)
        # 정렬 키가 시행일자 하나뿐이면, 조문별 단계 시행 때문에 **같은 시행일자에 여러 공포본**이
        # 있을 때 그 그룹의 가장 오래된 공포본이 선택된다. 실측(2026-09-01): 근로기준법
        # 시행일자 20070701에 2건이 있어 제24조 조회가 2006.12.21 공포본(구 "근로조건의 명시")을
        # 반환했다 — 정답은 2007.1.26 공포본(서면 명시·교부 의무 신설).
        # 같은 이름의 옛 ID·새 ID 행이 함께 남으면(폐지 후 재제정) 옛 법의 폐지 행과 새 법의 제정 행이 같은
        # 시행일자·공포번호로 겹친다 — 폐지 행을 앞에 두어 '기준일 이하 마지막 행'이 새 법이 되게 한다
        rows.sort(key=lambda r: (r.get("시행일자", ""), r.get("공포일자", ""),
                                 str(r.get("공포번호", "")).zfill(10),
                                 0 if "폐지" in (r.get("제개정구분") or "") else 1))
        return rows

    # (2026-10-09) 헤더 <조문시행일자문자열>로 이전 시행본에 소급하던 staged_article_dates는 뺐다 — 폴백 경로
    # (통합본 조회 실패)에서만 쓰였는데, 소급한 공포본 원문에도 시행 전 문안이 이미 들어 있어 실측 5건이 모두
    # 틀렸고(근로기준법 제101조·제13조·제44조의4·제105조, 노인장기요양보험법 제30조) '소급 선택했습니다'라는
    # 주의가 틀린 문안을 맞는 것처럼 보이게 했다. 폴백은 이제 그 시행본 MST의 공포 당시 원문 + 주의뿐이다.

    # ---------- 법령: 특정 시행본의 조문 원문 ----------
    @staticmethod
    def _version_xml(mst: str, ef_yd: str = "", alt_msts=()):
        """시행본(연혁 행 = MST + 시행일자)의 원문 XML. 반환 (xml, 주의 문구 or "").

        ef_yd(그 행의 시행일자)를 주면 시행일 기준 통합본(target=eflaw)을 받는다. 통합본 조회가
        실패하면(HTTP 오류, '일치하는 법령이 없습니다', 조문 없는 오류 화면) alt_msts(같은 시행일자의 다른
        MST — 통합본 문안이 같다)로 한 번씩 더 시도하고, 그래도 안 되면 그 MST 공포 당시 원문
        (target=law)으로 폴백하고 주의 문구를 돌려준다. 접속 실패·인증 실패는 폴백하지 않고 그대로
        올린다 — target=law도 같은 원천이라 결과가 같고, 부존재로 오판되면 안 되기 때문.
        ef_yd가 없으면 target=law (MST만 아는 호출 — 공포 당시 원문)."""
        if not ef_yd:
            return _get("lawService.do", target="law", MST=mst), ""
        reason = ""
        for m in (mst,) + tuple(alt_msts)[:2]:
            try:
                xml = _get("lawService.do", target="eflaw", MST=m, efYd=ef_yd)
                if "<조문단위" in xml:
                    return xml, ""
                reason = "일치하는 법령 없음" if _NO_MATCH_TEXT in xml[:500] else "조문 없는 응답"
            except LawUpstreamError as e:
                # _get은 requests 예외를 LawUpstreamError로 감싼다 — HTTP 상태 오류(500 등)만 폴백
                if not isinstance(e.__cause__, requests.exceptions.HTTPError):
                    raise
                code = getattr(e.__cause__.response, "status_code", "")
                reason = f"HTTP {code}".strip()
        xml = _get("lawService.do", target="law", MST=mst)
        return xml, (f"시행일 기준 문안 확인 실패 — 통합본(MST {mst}, 시행일자 {ef_yd}) 조회 실패({reason})로 그 MST "
                     f"공포 당시 원문으로 대신 조회함. 이 원문에는 그 뒤에 공포됐지만 이미 시행된 개정이 "
                     f"빠졌거나 아직 시행 전인 문안이 들어 있을 수 있어 기준일 현재 문안이 아닐 수 있으니 "
                     f"국가법령정보센터(law.go.kr)에서 시행일 기준 문안을 확인할 것")

    @staticmethod
    def _find_article_block(xml: str, article_no: str):
        """법령 원문 XML에서 조문 블록을 슬라이스. 반환 (조문 라벨, 블록 or None)."""
        # 조문 뒤에 <부칙> 섹션(수십만 자)이 이어지므로 슬라이스 범위에서 잘라낸다 —
        # 안 자르면 마지막 조문 조회 시 부칙 전체가 본문에 딸려 나온다. 부칙은 law_addenda로.
        cut = xml.find("<부칙>")
        if cut != -1:
            xml = xml[:cut]
        base_no, branch_no = article_no.strip(), None
        m = re.match(r"^(\d+)의(\d+)$", article_no.strip())
        if m:
            base_no, branch_no = m.group(1), m.group(2)
        label = f"제{base_no}조" + (f"의{branch_no}" if branch_no else "")
        # <조문번호> 위치 순차 슬라이스 (단순 <조문>…</조문> 정규식은 실패 — 2026-07-21 확인)
        positions = [(mm.start(), mm.group(1)) for mm in re.finditer(r"<조문번호>(\d+)</조문번호>", xml)]
        for i, (pos, no) in enumerate(positions):
            if no != base_no:
                continue
            end = positions[i + 1][0] if i + 1 < len(positions) else len(xml)
            block = xml[pos:end]
            bm = re.search(r"<조문가지번호>(\d+)</조문가지번호>", block[:300])
            blk_branch = bm.group(1) if bm else None
            if branch_no != blk_branch:
                continue
            # 절/관/장이 이 조문에서 시작되면 그 표제가 동일 <조문번호>를 단 채
            # 실제 조문보다 먼저 나온다(예: 제104조 앞의 "제6절 …" 노드) — 표제 블록은
            # 조번호 문자열 자체를 포함하지 않으므로 걸러내고 다음 후보를 찾는다.
            if label not in _strip_tags(_strip_cdata(block)):
                continue
            return label, block
        return label, None

    # 조문 블록 앞머리의 메타데이터 태그 — 태그만 지우면 값이 본문 앞에 줄로 남는다
    # ('1 / 조문 / 목적 / 20260102 / 일부개정 / 000000 / N / 제1조(목적) …', 2026-10-10 실측).
    # 조문 번호·제목은 본문 첫 줄('제1조(목적)')과 응답의 조문·조문제목 필드에 이미 있다.
    _ARTICLE_META_RE = re.compile(
        r"<(조문번호|조문가지번호|조문여부|조문제목|조문시행일자|조문제개정유형|조문이동이전|조문이동이후|조문변경여부|조문키)>"
        r".*?</\1>\s*|<(?:조문번호|조문가지번호|조문여부|조문제목|조문시행일자|조문제개정유형|조문이동이전|조문이동이후|조문변경여부|조문키)\s*/>\s*",
        re.S)
    # 항·호·목 번호 태그가 곧바로 이어지는 내용 태그의 첫머리와 같을 때('①' 다음 '①종합소득이…')만 번호를 뺀다
    _NUMBER_DUP_RE = re.compile(r"<(항|호|목)번호>(.+?)</\1번호>\s*(?=<\1내용>\s*\2)", re.S)

    @classmethod
    def _block_text(cls, block: str) -> str:
        """조문 블록 → 사람이 읽는 원문 (개정 주석 〈…〉 보존, 메타데이터 줄·중복 번호 줄 제외)."""
        t = cls._ARTICLE_META_RE.sub("", _strip_cdata(block))
        return _strip_tags(_keep_notes(cls._NUMBER_DUP_RE.sub("", t))).strip()

    @staticmethod
    def _article_content_lines(block: str):
        """대조용: 조문 블록에서 실제 문안(조문내용·항·호·목)만 줄 단위로 (메타데이터 제외, 주석 보존)."""
        lines = []
        for _tag, val in _CONTENT_TAG_RE.findall(block):
            for ln in _strip_tags(_keep_notes(_strip_cdata(val))).splitlines():
                if ln.strip():
                    lines.append(ln.strip())
        return lines

    @classmethod
    def _staged_parts(cls, block: str, base_date: str):
        """조문 블록의 [시행일] 표시 중 base_date보다 늦은 것 (시행예정_부분).
        통합본은 조문 일부(항·호)만 늦게 시행되면 그 문안을 미리 싣고 이렇게 표시한다.
        base_date는 기준일이다 — law_article_as_of는 시행본 시행일자가 아니라 as_of_date를 넘긴다
        (단계 시행일에 연혁 행이 없는 경우가 있어 시행본 날짜로 비교하면 기준일에 이미 시행 중인 부분을
        '아직 시행 전'으로 표시한다 — 2026-10-09 실측: 산업안전보건법 [시행일] 2028.1.1에 연혁 행 없음)."""
        if not base_date:
            return []
        return [s for s in _staged_notes(cls._block_text(block)) if s["시행일자"] > base_date]

    def law_article(self, mst: str, article_no: str, max_chars: int = 6000, ef_yd: str = "",
                    as_of: str = "", alt_msts=()):
        """시행본(연혁 행 = MST + 시행일자)의 조문 원문을 조번호로 슬라이스.
        ef_yd(그 행의 시행일자)를 주면 시행일 기준 통합본(target=eflaw&MST&efYd)에서 자른다 —
        연혁 행·현행 검색 결과에서 고른 시행본이면 반드시 ef_yd를 넘길 것. 없으면 그 MST 공포
        당시 원문(target=law&MST)이라 늦게 공포된 개정이 빠지거나 시행 전 문안이 섞일 수 있다.
        as_of: '시행예정_부분' 판정 기준일 (생략 시 시행본 시행일자).
        조문이 없으면 LawNotFound — 단 통합본 조회에 실패해 공포 당시 원문으로 대신 봤다면 LawUpstreamError
        (늦게 공포된 개정이 신설한 조문은 그 MST 원문에 없으므로 부존재로 단정할 수 없다).
        article_no: '17' 또는 '17의2' (패딩 없음)."""
        xml, caution = self._version_xml(mst, ef_yd, alt_msts)
        hdr = re.search(r"<시행일자>(\d+)</시행일자>", xml)  # 첫 <시행일자> = 기본정보(시행본 시행일자)
        label, block = self._find_article_block(xml, article_no)
        if block is None:
            if caution:
                raise LawUpstreamError(
                    f"MST {mst}(시행일자 {ef_yd})의 시행일 기준 통합본을 받지 못해 공포 당시 원문으로 대신 봤으나 "
                    f"{label} 조문이 없음 — 조문 부존재로 단정할 수 없음. 잠시 후 다시 조회하세요")
            where = f"MST {mst}" + (f"(시행일자 {ef_yd} 통합본)" if ef_yd else "")
            raise LawNotFound(f"{where}에서 {label} 조문을 찾지 못함")
        body = self._block_text(block)
        title = re.search(r"<조문제목>(.*?)</조문제목>", block, re.S)
        out = {
            "조문": label,
            "조문제목": _strip_cdata(title.group(1)) if title else "",
        }
        # <조문시행일자>는 통합본에서는 모든 조문이 그 시행본의 시행일자(efYd)라 조문별 시행일이 아니다
        # (2026-10-09 실측: labor 12개 법령 시행본 약 70개, 불일치 0건) — '조문시행일자'로 내보내면
        # '제101조는 2026.10.8 시행'처럼 잘못 읽힌다. 시행본 날짜는 적용시행본.시행일자에 있다.
        # 공포 당시 원문(ef_yd 없음·폴백)에서는 원천 태그 그대로 둔다.
        if not ef_yd or caution:
            em = re.search(r"<조문시행일자>(\d+)</조문시행일자>", block)
            if em:
                out["조문시행일자"] = em.group(1)
        out["원문"] = body[:max_chars]
        if len(body) > max_chars:
            out["잘림"] = (f"전체 {len(body)}자 중 앞 {max_chars}자만 표시됨 — "
                         f"max_chars를 {len(body)} 이상으로 지정해 다시 조회하면 전문을 볼 수 있음")
        _add_caution(out, caution)
        staged = self._staged_parts(block, as_of or ef_yd or (hdr.group(1) if hdr else ""))
        if staged:
            out["시행예정_부분"] = staged
            _add_caution(out, _staged_caution(staged))
        return out

    # ---------- 부칙 (시행일·적용례·경과조치) ----------
    @staticmethod
    def _split_addendum(text: str):
        """부칙 본문을 '제N조(…)' 항 단위로 분할. 조 편제 없는 단순 부칙은 통짜 1개.
        구식 '제1조 (시행일)'처럼 조와 괄호 사이 공백이 있는 표기도 같은 앵커로 잡힌다."""
        starts = [m.start() for m in re.finditer(r"(?m)^제\d+조(?:의\d+)?\s*\(", text)]
        if not starts:
            t = text.strip()
            return [t] if t else []
        parts = []
        head = text[:starts[0]].strip()
        if head:
            parts.append(head)  # "부칙 <제21221호,2025.12.23>" 머리말
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(text)
            parts.append(text[s:e].strip())
        return parts

    @staticmethod
    def _effective_summary(body: str, limit: int = 350):
        """부칙에서 시행일 부분만 요약 — 첫 '시행한다' 줄 + '각 호' 단서면 이어지는 호 목록."""
        m = re.search(r"(?m)^\s*(.*?시행한다.*)$", body)
        if not m:
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            return first[:limit]
        lines = [m.group(1).strip()]
        if "각 호" in lines[0]:
            for ln in body[m.end():].splitlines():
                t = ln.strip()
                if not t:
                    continue
                if re.match(r"^\d+\.", t):
                    lines.append(t)
                else:
                    break
        out = " ".join(lines)
        return out[:limit] + ("…" if len(out) > limit else "")

    @staticmethod
    def _parse_addenda_units(xml: str):
        """시행본 원문 XML에서 <부칙단위> 목록을 파싱 (law_addenda와 공포본 부칙 폴백이 공유)."""
        units = []
        for m in re.finditer(r"<부칙단위[^>]*>(.*?)</부칙단위>", xml, re.S):
            blk = m.group(1)
            dt = re.search(r"<부칙공포일자>(\d+)</부칙공포일자>", blk)
            no = re.search(r"<부칙공포번호>(\d+)</부칙공포번호>", blk)
            bd = re.search(r"<부칙내용>(.*?)</부칙내용>", blk, re.S)
            body = _strip_cdata(bd.group(1)) if bd else ""
            # 머리말 "부칙 <제21221호,2025.12.23>"의 꺾쇠가 태그 제거에 지워지지 않게 보존
            body = re.sub(r"<(제\d+호[^>]*?)>", r"〈\1〉", body)
            body = _strip_tags(body)
            body = re.sub(r"[ \t]+\n", "\n", body)
            body = re.sub(r"\n{2,}", "\n", body)  # CDATA 조각 사이 빈 줄 압축
            units.append({
                "공포일자": dt.group(1) if dt else "",
                "공포번호": no.group(1).lstrip("0") if no else "",  # "04803" 식 패딩 제거
                "본문": body,
            })
        return units

    def _addendum_from_promulgation(self, law_name: str, law_id: str, promul_no: str, history=None):
        """공포번호의 부칙을 그 공포본(MST) 공포 당시 원문에서 찾는다 — 자기 부칙은 반드시 들어 있다.
        시행일 기준 통합본에는 그날까지 시행된 개정의 부칙만 있어, 공포됐지만 아직 시행 전인 개정은
        통합본에서 찾을 수 없다(2026-10-09 실측: 근로기준법 20261008 통합본 부칙 30건 — 2027.1.1
        시행 예정인 2026.3.17 타법개정 부칙이 없고, MST 285279 원문에는 있음).
        반환 (부칙 단위, 그 공포번호의 연혁 행 목록) 또는 None."""
        if not law_name:
            return None
        if history is None:
            history = self.law_history(law_name, law_id=law_id)
        rows = [r for r in history if str(r.get("공포번호") or "").lstrip("0") == promul_no]
        if not rows:
            return None
        units = self._parse_addenda_units(_get("lawService.do", target="law", MST=rows[0]["MST"]))
        unit = next((u for u in reversed(units) if u["공포번호"] == promul_no), None)
        return (unit, rows) if unit else None

    def law_addenda(self, mst: str = "", law_name: str = "", law_id: str = "",
                    as_of_date: str = "", promul_no: str = "", article_no: str = "",
                    recent: int = 10, max_chars: int = 8000):
        """법령 부칙(附則) 조회 — 개정규정의 시행일·적용례·경과조치.
        시행본 원문의 <부칙단위> 목록을 파싱한다 (2026-08-26 실측: 소득세법 현행본에 부칙
        114건, 각각 공포일자·공포번호·본문 보유).

        - 시행본(연혁 행 = MST + 시행일자)의 시행일 기준 통합본에서 부칙을 읽는다. 통합본에는 그날까지
          시행된 개정의 부칙만 있다 (그 MST보다 늦게 공포됐지만 먼저 시행된 개정의 부칙은 들어 있고,
          공포됐지만 아직 시행 전인 개정의 부칙은 없다 — 2026-10-09 실측)
          · law_name: as_of_date를 주면 그 시점 시행본, 없으면 현행 시행본
          · mst: 그 MST의 연혁 행 중 기준일(as_of_date, 없으면 오늘) 이하 마지막 행 (예전에는 MST 공포 당시
            원문이라 늦게 공포·먼저 시행된 개정의 부칙이 빠지고 as_of_date도 무시했다)
        - 기준일 현재 공포됐지만 아직 시행 전인 개정은 연혁에서 찾아 따로 알린다 — 목록 모드는 '시행전_개정',
          article_no 모드는 그 개정들의 부칙(공포본 원문)에서 조문 언급을 '시행전_개정_부칙_언급'으로 발췌
          (2026-10-09 실측: 근로기준법 article_no='43의6'에서 2027.1.1 시행 예정 제21472호 부칙 제9조가 빠지고
          '별도 적용례가 없었다는 뜻일 수 있음'으로 안내됨)
        - promul_no: 그 공포번호 개정의 부칙 전문 (공포번호는 law_history_search 목록에 있음).
          시행본에 없으면(아직 시행 전 등) 그 공포본 원문에서 찾는다. '아직 시행 전' 판정은 기준일
          (as_of_date, 없으면 오늘) 기준이다
        - article_no: 전체 부칙에서 그 조문이 언급된 항(적용례·경과조치)만 최신순 발췌
        - 둘 다 없으면: 최신순 recent건 목록 (공포일자·공포번호·시행일 요약)
        """
        as_of_date = _norm_date(as_of_date, "as_of_date")
        if article_no:
            article_no = _norm_article_no(article_no)
        ref = as_of_date or _today_kst()   # '아직 시행 전' 판정 기준일
        ef_yd, history, pre_caution = "", None, ""
        if not mst:
            if not law_name:
                raise LawInvalidInput("mst 또는 law_name 중 하나는 필요합니다")
            if as_of_date:
                history = self.law_history(law_name, law_id=law_id)
                if history and not law_id:
                    _require_exact_name(history, law_name)
                chosen = None
                for row in history:  # 시행일자 오름차순
                    if row["시행일자"] and row["시행일자"] <= as_of_date:
                        chosen = row
                if not chosen:
                    raise LawNotFound(f"{as_of_date} 이전 시행본 없음: {law_name}")
                mst, ef_yd = chosen["MST"], chosen["시행일자"]
            else:
                # 이름이 정확히 같은 현행 법령만 쓴다 — 예전에는 정확 일치가 없으면 검색 첫 결과로 내려가
                # law='고용보험' 부칙이 보험료징수법 부칙으로 OK가 됐다 (2026-10-09 실측, nts에서는 '상법' →
                # '1980년해직공무원의보상등에관한특별조치법'. display=20이면 '상법'이 '…보상…' 20건에 밀린다)
                found = self.search_laws(law_name, display=100)
                rows = ([r for r in found if r.get("법령ID") == law_id] if law_id else
                        [r for r in found if _norm_name(r.get("법령명", "")) == _norm_name(law_name)])
                if not rows:
                    if not found:
                        raise LawNotFound(f"법령 검색 결과 없음: {law_name}")
                    names = []
                    for r in found:
                        if r.get("법령명") and r["법령명"] not in names:
                            names.append(r["법령명"])
                    raise LawInvalidInput(
                        (f"법령ID {law_id}인 현행 법령이 '{law_name}' 검색 결과에 없음" if law_id else
                         f"'{law_name}'과(와) 이름이 정확히 같은 현행 법령이 검색 결과에 없음")
                        + f" — 정식 법령명이나 통칭으로 다시 조회하세요 (검색된 법령: "
                        + ", ".join(names[:10]) + (" 등" if len(names) > 10 else "") + ")")
                chosen = rows[0]
                # 현행 검색 결과의 시행일자도 연혁 행의 시행일자다 (2026-10-09 실측: 소득세법 현행
                # MST 280405 시행일자 20260701 = 연혁 '기준일 이하 마지막 행')
                mst, ef_yd = chosen["MST"], chosen.get("시행일자", "")
                if not ef_yd:   # 시행일자가 비면 연혁에서 그 MST의 행을 찾는다 — 주의 없이 공포본으로 가지 않게
                    history = self.law_history(chosen.get("법령명") or law_name,
                                               law_id=chosen.get("법령ID") or law_id)
                    ef_yd = self._row_date_for_mst(history, mst, ref)
                    if not ef_yd:
                        pre_caution = (f"현행 검색 결과(MST {mst})에 시행일자가 없고 연혁 행도 찾지 못해 그 MST "
                                       f"공포 당시 원문의 부칙을 보여 줌 — 그 뒤에 공포·시행된 개정의 부칙은 없음")
        else:
            # MST만 오면 헤더의 법령명·법령ID로 연혁을 받아 그 MST의 연혁 행(기준일 이하 마지막 시행일자)을 쓴다
            head = _get("lawService.do", target="law", MST=mst)
            hname = re.search(r"<법령명_한글>(.*?)</법령명_한글>", head, re.S)
            hname = _strip_cdata(hname.group(1)) if hname else ""
            if law_name or hname:
                history = self.law_history(law_name or hname, law_id=law_id or self._header_law_id(head))
                ef_yd = self._row_date_for_mst(history, mst, ref)
            if not ef_yd:
                pre_caution = (f"MST {mst}의 연혁 행을 찾지 못해 그 MST 공포 당시 원문의 부칙을 보여 줌 — 그 뒤에 "
                               f"공포·시행된 개정의 부칙은 없음. 현행·기준일 기준은 법령명(+as_of_date)으로 조회")
        xml, caution = self._version_xml(mst, ef_yd)
        name = re.search(r"<법령명_한글>(.*?)</법령명_한글>", xml, re.S)
        enf = re.search(r"<시행일자>(\d+)</시행일자>", xml)
        units = self._parse_addenda_units(xml)
        if not units:
            raise LawNotFound(f"MST {mst} 응답에 부칙 없음 — 응답 앞부분: {xml[:200]}")
        out = {
            "법령명": _strip_cdata(name.group(1)) if name else "",
            "MST": mst,
            "시행본_시행일자": enf.group(1) if enf else "",
            "부칙총수": len(units),
        }
        _add_caution(out, pre_caution)
        _add_caution(out, caution)
        # 조문별 시행일이 다른 개정이면 그 내역이 헤더에 요약돼 있다
        # (예: "20260701:제57조의2, … 20270101:제17조제3항, …") — 통합본에도 그 MST의 공포 당시
        # 일정이 그대로 실린다 (이미 반영된 부분도 남아 있음, 2026-10-09 실측)
        multi = re.search(r"<조문시행일자문자열>(.*?)</조문시행일자문자열>", xml, re.S)
        multi_txt = _strip_cdata(multi.group(1)) if multi else ""
        if multi_txt:
            out["조문별_상이한_시행일"] = multi_txt
        consolidated = bool(ef_yd) and not caution
        base = out["시행본_시행일자"] or ef_yd
        if consolidated:
            scope = (f"{base} 시행일 기준 통합본" + (" (시행 예정 시행본)" if base > _today_kst() else "")
                     + " — 그날까지 시행된 개정의 부칙만 들어 있음. 공포됐지만 아직 시행 전인 개정의 부칙은 "
                       "promul_no(공포번호)로 조회")
        else:
            scope = (f"MST {mst} 공포 당시 원문 — 이 공포본까지 공포된 개정의 부칙이며 그 뒤에 공포된 "
                     f"개정의 부칙은 없음. 현행·기준일 기준은 법령명(+as_of_date)으로 조회")

        if promul_no:
            want = promul_no.strip().lstrip("0") or promul_no.strip()
            hits = [u for u in units if u["공포번호"] == want]
            if not hits:
                found = self._addendum_from_promulgation(
                    law_name or out["법령명"], law_id or self._header_law_id(xml), want, history)
                if not found:
                    raise LawNotFound(
                        f"공포번호 {promul_no}의 부칙이 이 시행본에 없음 (부칙 {len(units)}건 보유) — "
                        f"mode='연혁'으로 공포번호를 확인하세요")
                unit, rows = found
                hits = [unit]
                dates = sorted({r["시행일자"] for r in rows if r.get("시행일자")})
                # '아직 시행 전'은 기준일(as_of_date, 없으면 오늘)로 판정한다 — 시행본 시행일자로 판정하면
                # 이미 시행 중인 개정을 '시행 전'으로, 기준일과 다른 날짜로 안내한다
                if dates and all(d > ref for d in dates):
                    tail = f" — 이 개정은 {ref} 현재 아직 시행 전 (연혁상 시행일자 {', '.join(dates)})"
                elif dates and base and all(d > base for d in dates):
                    tail = (f" — 이 개정은 이 시행본({base}) 뒤에 시행됨 (연혁상 시행일자 {', '.join(dates)}); "
                            f"그 뒤 시행본 기준은 as_of_date를 시행일 이후로 지정")
                else:
                    tail = ""
                _add_caution(out, f"공포번호 {want}의 부칙은 이 시행본({base or 'MST ' + mst})에 없어 그 공포본"
                                  f"(MST {rows[0]['MST']}) 원문에서 가져옴" + tail)
            u = hits[-1]
            body = u["본문"]
            out["부칙"] = {"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                          "본문": body[:max_chars]}
            out["부칙범위"] = scope
            if len(body) > max_chars:
                out["잘림"] = (f"이 부칙 전체 {len(body)}자 중 앞 {max_chars}자만 표시됨 — "
                             f"max_chars를 {len(body)} 이상으로 지정해 다시 조회")
            return out

        # 기준일 현재 공포됐지만 아직 시행 전인 개정 — 통합본 부칙에 없으므로 연혁에서 찾는다
        pending = []
        if consolidated:
            if history is None:
                history = self.law_history(law_name or out["법령명"], law_id=law_id or self._header_law_id(xml))
            pending = self._pending_promulgations(history, ref, {u["공포번호"] for u in units})

        if article_no:
            label = _article_label(article_no)
            pat = _article_pat(label)
            found, used = [], 0
            for u in reversed(units):  # 최신 개정부터
                paras = [p[:1500] for p in self._split_addendum(u["본문"]) if pat.search(p)]
                if not paras:
                    continue
                size = sum(len(p) for p in paras)
                if found and used + size > max_chars:
                    out["잘림"] = (f"{label} 언급 부칙이 더 있으나 max_chars({max_chars}) 초과로 "
                                 f"최신 {len(found)}건까지만 표시 — max_chars를 늘려 다시 조회")
                    break
                used += size
                found.append({"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                              "해당항": paras})
            out["조문"] = label
            out["언급된_부칙_최신순"] = found
            out["부칙범위"] = scope
            pfound = []
            if pending:
                pfound, unchecked = self._pending_mentions(history, pending, pat, ref, max_chars)
                if pfound:
                    out["시행전_개정_부칙_언급"] = pfound
                    _add_caution(out, (
                        f"기준일({ref}) 현재 공포됐지만 아직 시행 전인 개정("
                        + ", ".join(f"제{p['공포번호']}호 {'·'.join(p['연혁상_시행일자'])} 시행" for p in pfound)
                        + f")의 부칙에도 {label} 언급이 있음 — 통합본 부칙에는 아직 없어 공포본 원문에서 "
                          f"'시행전_개정_부칙_언급'으로 발췌함"))
                if unchecked:
                    _add_caution(out, f"시행 전 개정 {', '.join('제' + n + '호' for n in unchecked)}의 부칙은 확인 못 함 "
                                      f"— promul_no로 직접 조회")
            if not found and not pfound:
                out["안내"] = (f"{label}이(가) 언급된 부칙 조항 없음"
                             + (f" (시행 전 개정 {len(pending)}건의 부칙 포함)" if pending else "")
                             + " — 그 조문 개정에 별도 적용례·경과조치가 없었다는 뜻일 수 있음 (이때 시행일은 "
                               "각 시행본의 시행일자·부칙 제1조를 따름)")
            return out

        rows = []
        for u in reversed(units):  # 최신순
            rows.append({"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                         "시행일": self._effective_summary(u["본문"])})
            if len(rows) >= max(1, recent):
                break
        out["부칙목록_최신순"] = rows
        out["부칙범위"] = scope
        if pending:
            out["시행전_개정"] = [{k: p[k] for k in ("공포일자", "공포번호", "제개정구분", "연혁상_시행일자")}
                               for p in pending]
        if len(units) > len(rows) or pending:
            out["안내"] = ((f"전체 {len(units)}건 중 최신 {len(rows)}건만 표시 — recent를 늘리거나, "
                          if len(units) > len(rows) else "")
                         + "특정 개정의 부칙 전문은 promul_no로, 특정 조문의 적용시기는 article_no로 조회"
                         + (f". '시행전_개정'은 기준일({ref}) 현재 공포됐지만 아직 시행 전이라 이 통합본 부칙에 없는 "
                            f"개정 — 그 부칙은 promul_no로 조회" if pending else ""))
        return out

    @staticmethod
    def _header_law_id(xml: str) -> str:
        m = re.search(r"<법령ID>(\d+)</법령ID>", xml[:3000])
        return m.group(1) if m else ""

    @staticmethod
    def _row_date_for_mst(history, mst: str, ref: str) -> str:
        """연혁에서 그 MST 행 중 ref 이하 마지막 시행일자 (전부 ref 뒤면 가장 이른 행, 행이 없으면 "")."""
        rows = [r for r in history or [] if r.get("MST") == mst and r.get("시행일자")]
        if not rows:
            return ""
        elig = [r for r in rows if r["시행일자"] <= ref]
        return (elig[-1] if elig else rows[0])["시행일자"]

    @staticmethod
    def _last_promulgation(history, ref: str):
        """공포일자가 ref 이하인 마지막 공포본의 연혁 행 (없으면 None) — 기준일 당시 공포돼 있던 문안의 원천."""
        rows = [r for r in history or [] if r.get("공포일자") and r["공포일자"] <= ref and r.get("MST")]
        if not rows:
            return None
        return max(rows, key=lambda r: (r["공포일자"], str(r.get("공포번호") or "").zfill(10)))

    @staticmethod
    def _pending_promulgations(history, ref: str, have):
        """ref 현재 공포됐지만 아직 시행 전인 개정 — 공포번호의 연혁 행이 모두 ref보다 늦게 시행되고 통합본 부칙
        (have: 공포번호 집합)에 없는 것. 최신 공포순."""
        by_no = {}
        for r in history or []:
            no = str(r.get("공포번호") or "").lstrip("0")
            if no:
                by_no.setdefault(no, []).append(r)
        out = []
        for no, rows in by_no.items():
            dates = sorted({r["시행일자"] for r in rows if r.get("시행일자")})
            pubs = [r["공포일자"] for r in rows if r.get("공포일자")]
            if no in have or not dates or not pubs:
                continue
            if dates[0] > ref and min(pubs) <= ref:
                out.append({"공포일자": min(pubs), "공포번호": no, "제개정구분": rows[0].get("제개정구분", ""),
                            "연혁상_시행일자": dates, "MST": rows[0]["MST"]})
        out.sort(key=lambda p: (p["공포일자"], p["공포번호"].zfill(10)), reverse=True)
        return out

    def _pending_mentions(self, history, pending, pat, ref: str, max_chars: int):
        """시행 전 개정들의 부칙에서 조문 언급 항을 발췌 — 기준일 이전 마지막 공포본 원문 하나에 그때까지 공포된
        개정의 부칙이 다 들어 있다. 반환 (발췌 목록, 확인 못 한 공포번호 목록)."""
        latest = self._last_promulgation(history, ref)
        units = []
        if latest:
            try:
                units = self._parse_addenda_units(_get("lawService.do", target="law", MST=latest["MST"]))
            except LawUpstreamError as e:
                if not isinstance(e.__cause__, requests.exceptions.HTTPError):
                    raise
                units = []
        umap = {u["공포번호"]: u for u in units}
        found, unchecked, used = [], [], 0
        for p in pending:
            u = umap.get(p["공포번호"])
            if u is None:
                unchecked.append(p["공포번호"])
                continue
            paras = [x[:1500] for x in self._split_addendum(u["본문"]) if pat.search(x)]
            if not paras:
                continue
            size = sum(len(x) for x in paras)
            if found and used + size > max_chars:
                unchecked.append(p["공포번호"])
                continue
            used += size
            found.append({"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                          "연혁상_시행일자": p["연혁상_시행일자"], "해당항": paras})
        return found, unchecked

    # ---------- 행정규칙 (훈령·예규·고시·기본통칙) ----------
    def search_admin_rules(self, keyword: str = "", display: int = 10, page: int = 1,
                           org: str = "", kind: str = "", search_body: bool = False,
                           sort: str = "", current_only: bool = True):
        """target=admrul 행정규칙 검색 — 기본통칙·조사사무처리규정·고시 등.

        - org: 소관부처 코드 또는 이름("고용노동부" → 1492000). 비우면 전 부처.
        - kind: 훈령·예규·고시·공고·지침·기타 (law.go.kr knd 1~6). 비우면 전체.
        - search_body: True면 본문 검색(search=2), 기본은 규칙명 검색.
        - sort: "" 관련도 | "ddes" 발령일 내림차순 | "efdes" 시행일 내림차순.
        - current_only: 현행만(nw=1). False면 연혁 포함(nw=2).
        - keyword를 비우면 org/kind 필터만으로 전체 목록 (예: 고용노동부 현행 441건, 2026-09-03).
        반환 dict: {"total", "items": [...]} — 예전 list 반환은 items로 옮겼다.
        """
        org_code = ADMRUL_ORG_CODES.get(org.strip(), org.strip()) if org else ""
        if org and not org_code.isdigit():
            raise LawInvalidInput(
                f"소관부처를 인식하지 못했습니다: {org!r} — 이름({', '.join(ADMRUL_ORG_CODES)}) "
                f"또는 law.go.kr 부처코드 숫자를 넣으세요")
        knd = ""
        if kind:
            knd = ADMRUL_KIND_CODES.get(kind.strip(), kind.strip())
            if knd not in {"1", "2", "3", "4", "5", "6"}:
                raise LawInvalidInput(
                    f"행정규칙 종류를 인식하지 못했습니다: {kind!r} — {', '.join(ADMRUL_KIND_CODES)} 중 하나")
        params = dict(target="admrul", query=(keyword or "").strip(), display=min(int(display), 100),
                      page=page, search=2 if search_body else 1, org=org_code, knd=knd,
                      nw=1 if current_only else 2, sort=sort)
        xml = _get("lawSearch.do", **params)
        m = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        items = _parse_items(xml, "admrul")
        rows = [{
            "일련번호": it.get("행정규칙일련번호", ""),
            "행정규칙명": it.get("행정규칙명", ""),
            "종류": it.get("행정규칙종류", ""),
            "소관부처": it.get("소관부처명", ""),
            "발령일자": it.get("발령일자", ""),
            "발령번호": it.get("발령번호", ""),
            "시행일자": it.get("시행일자", ""),
            "현행연혁": it.get("현행연혁구분", ""),
            "제개정": it.get("제개정구분명", ""),
        } for it in items]
        return {"total": int(m.group(1)) if m else len(rows), "items": rows}

    def get_admin_rule(self, serial: str, max_chars: int = 10000,
                       article: str = "", start_char: int = 0):
        """target=admrul 행정규칙 본문.
        article: 조번호를 주면 해당 조문만 슬라이스 — "9-5"(외국환거래규정식 제9-5조),
        "23", "23의2" 형식. 외국환거래규정(30만 자) 같은 대형 고시는 전문 반환이
        불가능하므로 조번호 지정이 사실상 필수. start_char: 조번호를 모를 때
        해당 위치부터 max_chars만큼 이어 읽는 오프셋."""
        xml = _get("lawService.do", target="admrul", ID=serial)
        name = re.search(r"<행정규칙명>(.*?)</행정규칙명>", xml, re.S)
        body = _strip_tags(_strip_cdata(xml))
        if len(body) < 50:
            return {"오류": "본문 없음", "응답": xml[:200]}
        out = {
            "행정규칙명": _strip_cdata(name.group(1)) if name else "",
            "본문길이": len(body),
        }
        if article:
            a = article.strip()
            am = re.match(r"^([\d-]+)(의\d+)?$", a)
            base, ui = (am.group(1), am.group(2) or "") if am else (a, "")
            # 조문 표제는 행 첫머리 "제9-5조(제목)" 형태 — 본문 중 인용("제7-31조의 규정 …")과
            # 행 앵커로 구분한다. 표제가 행 중간에 오는 규칙이면 못 찾으므로 start_char로 폴백.
            m = re.search(rf"(?m)^제{re.escape(base)}조{ui}(?=\(|\s|$)", body)
            if not m:
                out["오류"] = (f"제{a}조 표제를 찾지 못함 — 조번호 형식(예: '9-5', '23', '23의2')을 "
                             f"확인하거나 start_char 오프셋으로 조회")
                return out
            nm = re.search(r"(?m)^제[\d-]+조(?:의\d+)?\s*(?=\()", body[m.end():])
            seg = body[m.start():m.end() + nm.start()] if nm else body[m.start():]
            out["조문"] = f"제{a}조"
            out["본문"] = seg[:max_chars]
            if len(seg) > max_chars:
                out["잘림"] = (f"이 조문 전체 {len(seg)}자 중 앞 {max_chars}자만 표시됨 — "
                             f"max_chars를 {len(seg)} 이상으로 지정해 다시 조회")
            return out
        window = body[start_char:start_char + max_chars]
        out["본문"] = window
        if start_char + len(window) < len(body):
            out["잘림"] = (f"전체 {len(body)}자 중 {start_char}~{start_char + len(window)}자 구간만 표시됨 — "
                         f"start_char={start_char + len(window)}로 이어서 조회하거나, "
                         f"article에 조번호(예: '9-5')를 지정하면 해당 조문만 반환")
        return out

    # ---------- 조세조약 등 조약 ----------
    def search_treaties(self, keyword: str, display: int = 10, page: int = 1):
        """target=trty 조약 검색 — 조세조약 원문·발효일 확인용."""
        xml = _get("lawSearch.do", target="trty", query=keyword, display=display, page=page)
        items = _parse_items(xml, "Trty") + _parse_items(xml, "trty")
        return [{
            "조약일련번호": it.get("조약일련번호", ""),
            "조약명": it.get("조약명한글", it.get("조약명", "")),
            "조약구분": it.get("조약구분명", ""),
            "서명일자": it.get("서명일자", ""),
            "발효일자": it.get("발효일자", ""),
        } for it in items]

    def get_treaty(self, serial: str, max_chars: int = 12000):
        """target=trty 조약 본문."""
        xml = _get("lawService.do", target="trty", ID=serial)
        name = re.search(r"<조약명한글>(.*?)</조약명한글>", xml, re.S)
        body = _strip_tags(_strip_cdata(xml))
        if len(body) < 50:
            return {"오류": "본문 없음", "응답": xml[:200]}
        return {
            "조약명": _strip_cdata(name.group(1)) if name else "",
            "본문": body[:max_chars],
            "본문길이": len(body),
        }

    # ---------- 자치법규 (조례·규칙) ----------
    def search_ordinances(self, keyword: str, region: str = "", display: int = 20, page: int = 1):
        """target=ordin 자치법규 검색. region으로 지자체명 필터 (예: '서울', '용산구')."""
        xml = _get("lawSearch.do", target="ordin", query=keyword, display=display, page=page)
        items = _parse_items(xml, "law") + _parse_items(xml, "ordin")
        rows = []
        for it in items:
            org = it.get("지자체기관명", it.get("소관부처명", ""))
            if region and region not in org:
                continue
            rows.append({
                "일련번호": it.get("자치법규일련번호", it.get("법령일련번호", "")),
                "자치법규명": it.get("자치법규명", it.get("법령명한글", "")),
                "지자체": org,
                "공포일자": it.get("공포일자", ""),
                "시행일자": it.get("시행일자", ""),
                "제개정구분": it.get("제개정구분명", ""),
            })
        return rows

    def get_ordinance(self, serial: str, max_chars: int = 10000):
        """target=ordin 자치법규 본문."""
        xml = _get("lawService.do", target="ordin", ID=serial)
        name = re.search(r"<자치법규명>(.*?)</자치법규명>", xml, re.S)
        body = _strip_tags(_strip_cdata(xml))
        if len(body) < 50:
            return {"오류": "본문 없음", "응답": xml[:200]}
        return {
            "자치법규명": _strip_cdata(name.group(1)) if name else "",
            "본문": body[:max_chars],
            "본문길이": len(body),
        }

    # ---------- 편의: 특정 날짜 시행본의 조문 (예규 당시 조문 확인용) ----------
    def law_article_as_of(self, law_name: str, as_of_date: str, article_no: str, law_id: str = "",
                          max_chars: int = 6000):
        """as_of_date(YYYYMMDD) 당시 시행 중이던 시행본을 골라 해당 조문 원문 반환.
        예규·판례가 인용한 '당시 조문' 검증용 — 회신일을 넣으면 그 시점 법을 준다.

        기준일 이하 마지막 연혁 행(MST + 시행일자)의 시행일 기준 통합본 = 기준일 당시 시행 중이던 문안.
        통합본은 단계 시행을 날짜대로 반영하므로 단계 시행 소급을 하지 않는다 — 헤더 일정으로 소급하면 같은
        조문의 이미 시행된 개정까지 잃는다 (2026-10-09 실측: 근로기준법 제116조는 헤더상 20270101이지만
        20261008 통합본에 그날 시행분 개정이 있다. 통합본 조문의 <조문시행일자>는 노동법·세법 시행본 70개 전
        조문에서 efYd보다 늦은 것이 없었다). 통합본 조회가 실패하면 같은 시행일자의 다른 MST로 다시 시도하고,
        그래도 안 되면 그 행 MST의 공포 당시 원문 + 주의(소급하지 않음 — 예전 소급은 실측 5건이 모두 틀렸다).
        과거 기준일(오늘 이전)이면 통합본 시대착오를 점검해 그 시점 공포 문안을 '공포본_참고'로 함께 준다."""
        as_of_date = _norm_date(as_of_date, "as_of_date")
        if not as_of_date:
            raise LawInvalidInput("as_of_date가 필요합니다 (YYYYMMDD)")
        article_no = _norm_article_no(article_no)
        history = self.law_history(law_name, law_id=law_id)
        if not history:
            raise LawNotFound(f"연혁 없음: {law_name} — 정식 법령명인지 확인 (통칭은 근기법·산재보험법 등 지원분만)")
        if not law_id:
            _require_exact_name(history, law_name)
        candidates = [r for r in history if r["시행일자"] and r["시행일자"] <= as_of_date]
        if not candidates:
            first = history[0]
            kind = first.get("제개정구분") or ""
            hint = (" — 연혁 첫 행이 제정이 아니라 '" + (kind or "개정") + "'이라 그 전 시행본이 법제처 연혁에 없거나 옛 "
                    "법령ID(폐지 후 재제정 전의 법)로 따로 있을 수 있음 (같은 법령ID의 개명 전 시행본은 이미 포함해 찾았음 — "
                    "옛 법령명으로 다시 조회)"
                    if kind != "제정" else
                    (" — 이 법령ID의 제정 전이다. 폐지 후 재제정된 법(근로기준법 1997.3.13 재제정, 구법 법령ID 001769 등)은 "
                     "구법 시절 조문이 따로 있으니 옛 법령명·mode='연혁'으로 확인" if law_id else ""))
            raise LawNotFound(f"{as_of_date} 이전 시행본 없음 (연혁의 최초 시행 {first['시행일자']}"
                              f"{' ' + kind if kind else ''})" + hint)
        last = candidates[-1]
        alt = [r["MST"] for r in reversed(candidates) if r["시행일자"] == last["시행일자"] and r["MST"] != last["MST"]]
        past = as_of_date < _today_kst()
        try:
            art = self.law_article(last["MST"], article_no, max_chars, ef_yd=last["시행일자"],
                                   as_of=as_of_date, alt_msts=alt)
        except LawNotFound as e:
            if not past:
                # 통합본에 그 조문이 없다 = 기준일 현재 그 조문이 없다 (신설 전·조번호 오류)
                raise LawNotFound(
                    f"{as_of_date} 당시 시행본({last['시행일자']} 시행 통합본)에 {_article_label(article_no)} 조문이 없음 — "
                    f"아직 신설 전이거나 조번호 오류. 시행 예정 조문이면 as_of_date를 시행일 이후로, "
                    f"과거 조문이면 그 시점으로 지정") from e
            art = None   # 과거 통합본에 없는 조문 — 공포본 대조 전에는 부존재로 단정하지 않는다
        if past:
            art = self._check_promulgated(art, history, last, as_of_date, article_no, max_chars)
            if art.get("시행예정_부분"):
                _add_caution(art, self._STAGED_PAST_CAUTION)
        art["적용시행본"] = {k: last[k] for k in ("법령명", "시행일자", "공포일자", "공포번호", "제개정구분", "MST")}
        today = _today_kst()
        in_force = [r for r in history if r.get("시행일자") and r["시행일자"] <= today] or history
        latest_name = in_force[-1].get("법령명", "")   # 개명이 시행 예정이어도 '현재' 이름은 오늘 시행 중인 것
        if last.get("법령명") and latest_name and _norm_name(last["법령명"]) != _norm_name(latest_name):
            _add_caution(art, f"기준일({as_of_date}) 당시 법령명은 '{last['법령명']}'(현재 '{latest_name}', 같은 법령ID) — "
                              f"그 사이 조문 번호·체계가 달라졌을 수 있으니 조문 제목과 내용으로 확인")
        return art

    _STAGED_PAST_CAUTION = (
        "과거 기준일 조회 — 법제처 과거 통합본의 [시행일] 표시에는 그 뒤 부칙 개정으로 바뀐 시행 일정이 실려 있을 수 "
        "있음 (2026-10-09 실측: 소득세법 제81조의11 20230101 통합본 표시는 2026.1.1, 기준일 당시 부칙은 2024.1.1). "
        "기준일 당시 일정은 그 개정의 부칙(mode='부칙', promul_no)으로 확인")

    def _check_promulgated(self, art, history, chosen: dict, as_of: str, article_no: str, max_chars: int):
        """과거 기준일 조회의 통합본 시대착오 점검. 법제처 과거 통합본은 시행 전 규정이 뒤에 다시 개정되거나
        폐지되면 그 결과를 소급해 싣는다 (2026-10-09 실측, 세법: 소득세법 제81조의11 20240101·20250101 통합본에
        2025.12.23 개정 문안이 들어 있고 그 시점 공포본은 종전 문안 — labor 12개 법령 2025~2028 시행본에서는 사례
        0건, 그 밖의 시기는 확인 못 함). 그래서 기준일 이전 마지막 공포본(target=law)의 같은 조문을
        '공포본_참고'로 함께 준다:
          - 통합본 조문의 개정 주석에 기준일 뒤 날짜가 있으면 → '주의' + (문안이 다르면) 공포본_참고
          - 통합본에 조문이 없거나 '삭제'인데 공포본에는 있으면 → 공포본_참고 (기준일 당시 공포됐지만 시행 전)
        과거 통합본만 보고 NOT_FOUND(부존재 단정 가능)를 내지 않는다. art가 None이면 통합본에 조문이 없던 경우."""
        xml, caution = self._version_xml(chosen["MST"], chosen["시행일자"])
        if caution:   # 이미 공포 당시 원문으로 폴백 — 대조할 다른 원천이 없다
            return art
        label, block = self._find_article_block(xml, article_no)
        late, deleted = [], False
        if block is not None:
            late = sorted(d for d in self._block_note_dates(block) if d > as_of)
            deleted = self._is_deleted(block, label)
            if not late and not deleted:
                return art
        prom = self._last_promulgation(history, as_of)
        pblock, fetch_err = None, ""
        if prom:
            try:
                pxml = _get("lawService.do", target="law", MST=prom["MST"])
                _l, pblock = self._find_article_block(pxml, article_no)
            except LawUpstreamError as e:   # 접속 실패는 그대로 올리고 HTTP 상태 오류만 '확인 못 함'
                if not isinstance(e.__cause__, requests.exceptions.HTTPError):
                    raise
                fetch_err = f"HTTP {getattr(e.__cause__.response, 'status_code', '')}".strip()
        where = (f"기준일 이전 마지막 공포본(MST {prom['MST']}, {prom['공포일자']} 공포 "
                 f"제{str(prom.get('공포번호') or '').lstrip('0')}호)" if prom else "기준일 이전 공포본")
        if block is None:
            if pblock is None:
                if fetch_err:
                    raise LawUpstreamError(
                        f"{as_of} 당시 시행본({chosen['시행일자']} 시행 통합본)에 {label} 조문이 없고 {where} 조회도 "
                        f"실패({fetch_err}) — 조문 부존재로 단정할 수 없음")
                raise LawNotFound(f"{as_of} 당시 시행본({chosen['시행일자']} 시행 통합본)에도 {where}에도 "
                                  f"{label} 조문이 없음 — 그때 없던 조문이거나 조번호 오류")
            if self._is_deleted(pblock, label):
                plines = self._article_content_lines(pblock)
                raise LawNotFound(f"{label} 조문은 기준일({as_of}) 당시 삭제된 조문 — 시행일 기준 통합본에 없고 {where}에는 "
                                  f"'{plines[0][:80]}'")
            ptitle = re.search(r"<조문제목>(.*?)</조문제목>", pblock, re.S)
            out = {"조문": label, "조문제목": _strip_cdata(ptitle.group(1)) if ptitle else "", "원문": "",
                   "통합본_문안_없음": (f"{chosen['시행일자']} 시행일 기준 통합본에 {label} 조문이 없음 — 기준일({as_of}) "
                                    f"당시 시행 중인 문안이 아니었음")}
            out["공포본_참고"] = self._prom_ref(prom, pblock, as_of, max_chars)
            _add_caution(out, (f"기준일({as_of}) 당시 시행 중인 {label} 조문은 없음(시행일 기준 통합본). 다만 {where} 원문에는 "
                               f"이 조문이 있어 '공포본_참고'로 함께 줌 — 기준일 당시 공포됐지만 아직 시행 전이었거나, 그 뒤 "
                               f"시행 전에 폐지·개정되어 과거 통합본에서 빠진 규정일 수 있음. 시행일·경과는 "
                               f"mode='부칙'(promul_no·article_no)으로 확인"))
            return out
        differs = pblock is not None and (_norm_key(self._article_content_lines(pblock))
                                          != _norm_key(self._article_content_lines(block)))
        if differs:
            art["공포본_참고"] = self._prom_ref(prom, pblock, as_of, max_chars)
        if late:
            msg = (f"이 조문 통합본 문안에 기준일({as_of}) 뒤 날짜의 개정 주석({', '.join(late)})이 있음 — 법제처 과거 "
                   f"통합본은 시행 전 규정이 뒤에 다시 개정되면 그 결과를 소급해 싣기도 함. ")
            if differs:
                msg += (f"{where} 문안을 '공포본_참고'로 함께 줌 — 기준일 당시 공포돼 있던 문안은 그쪽이며, 차이 나는 "
                        f"부분은 기준일 뒤 개정분일 수 있음 (시행예정_부분의 시행일도 그 뒤 바뀐 일정일 수 있음)")
            elif pblock is not None:
                msg += f"다만 {where}의 이 조문 문안과는 같음"
            elif fetch_err:
                msg += f"{where} 대조는 확인 못 함 (조회 실패 {fetch_err})"
            else:
                msg += f"{where}에는 이 조문이 없음"
            _add_caution(art, msg)
        elif deleted and differs:
            _add_caution(art, (f"시행일 기준 통합본(기준일 당시 시행 문안)은 '{label} 삭제'이나 {where}에는 다른 문안이 "
                               f"있어 '공포본_참고'로 함께 줌 — 기준일 당시 공포됐지만 아직 시행 전이던 규정일 수 있음 (그 뒤 "
                               f"시행 전에 폐지·개정되면 과거 통합본에 실리지 않음)"))
        elif deleted and fetch_err:
            _add_caution(art, f"이 조문은 통합본에서 '삭제' — {where} 대조는 확인 못 함 (조회 실패 {fetch_err})")
        return art

    def _prom_ref(self, prom: dict, pblock: str, as_of: str, max_chars: int) -> dict:
        """'공포본_참고' — 기준일 이전 마지막 공포본(공포 당시 원문)의 같은 조문."""
        body = self._block_text(pblock)
        title = re.search(r"<조문제목>(.*?)</조문제목>", pblock, re.S)
        ref = {"MST": prom["MST"], "공포일자": prom["공포일자"],
               "공포번호": str(prom.get("공포번호") or "").lstrip("0"), "제개정구분": prom.get("제개정구분", ""),
               "조문제목": _strip_cdata(title.group(1)) if title else "", "원문": body[:max_chars]}
        if len(body) > max_chars:
            ref["잘림"] = f"공포본 조문 전체 {len(body)}자 중 앞 {max_chars}자만 표시됨 — max_chars를 늘려 다시 조회"
        staged = [s for s in _staged_notes(body) if s["시행일자"] > as_of]
        if staged:
            ref["시행예정_부분"] = staged
        return ref

    @classmethod
    def _block_note_dates(cls, block: str):
        """조문 블록의 개정 주석 날짜(YYYYMMDD) 집합 — 〈개정 …〉·〈신설 …〉·삭제 〈…〉와 [본조신설 …]·[전문개정 …]
        ·[종전 …] 등 (시행일 표시·결정 주석 제외)."""
        t = cls._block_text(block)
        out = set()
        for note in re.findall(r"〈[^〉]*〉", t) + _BRACKET_NOTE_RE.findall(t):
            for y, m, d in _NOTE_DATE_RE.findall(note):
                out.add(f"{y}{int(m):02d}{int(d):02d}")
        return out

    @classmethod
    def _is_deleted(cls, block: str, label: str) -> bool:
        """조문이 '제N조 삭제 〈…〉' 꼴인지."""
        lines = cls._article_content_lines(block)
        return bool(lines) and bool(re.match(re.escape(label) + r"\s*(?:\([^)]*\))?\s*삭제", lines[0]))
