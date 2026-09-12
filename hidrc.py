# -*- coding: utf-8 -*-
"""
hidrc.py — 건강보험분쟁조정위원회 재결례 클라이언트 (simpan.go.kr 온라인행정심판)

국민건강보험공단·건강보험심사평가원의 처분에 대한 **심판청구** 재결례다. 건강보험은
이의신청(공단/심평원) → 심판청구(건강보험분쟁조정위원회) → 행정소송으로 이어지는
특별행정심판이라, 중앙행정심판위원회 재결례(law.go.kr target=decc)에는 거의 오지 않는다.
정산보험료 부과·피부양자 자격·보험료 경감처럼 실무에서 자주 걸리는 쟁점의 유일한 공개
판단례여서 따로 긁는다. (2026-09-12 실측: 전체 68건, 2017~2025년.)

경로 — 위원회 통합홈페이지의 재결례 검색이 쓰는 AJAX를 그대로 호출한다:
  1) GET  /eas/aa/ah/300/list.do?cmnMenuCd=E10040000        세션 쿠키 확보
  2) POST /eas/aa/ah/300/selectBfrList.do                   목록 (JSON, 10건/페이지)
  3) POST /eas/aa/ah/300/download.do                        첨부 PDF의 실제 주소를 받고
  4) GET  <그 주소>                                          PDF 본문 → pypdf로 텍스트 추출
  요청은 JSON 본문이어야 하고 X-CSRF-HEADER 헤더가 있어야 한다. 값은 서버가 검증하지 않는
  임의 난수(사이트 스크립트가 매 요청 새로 만든다)라 같은 방식으로 만들어 보낸다.

위원회 코드(2026-09-12 실측): 보상보험 대분류 40150002 / 중분류 40090007 아래
  건강보험분쟁조정위원회 40100009 · 장기요양재심사위원회 40100010 · 국민연금재심사위원회 40100011.
장기요양·국민연금 두 위원회는 이 포털에 공개한 재결례가 0건이라 지금은 건강보험만 받는다
(국민연금 쪽 사례는 nps_review.py가 공단 게시판에서 따로 받는다).
"""
import base64
import io
import logging
import random
import re
import string
import time

import requests

logger = logging.getLogger("hidrc")

BASE = "https://simpan.go.kr"
MENU = "E10040000"
SEED_URL = f"{BASE}/eas/aa/ah/300/list.do?cmnMenuCd={MENU}"
LIST_URL = f"{BASE}/eas/aa/ah/300/selectBfrList.do"
DOWNLOAD_URL = f"{BASE}/eas/aa/ah/300/download.do"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 60
PAGE_SIZE = 10                      # 서버가 정한 값 — 인자로 못 바꾼다

COMMITTEES = {
    "건강보험분쟁조정위원회": "40100009",
    "장기요양재심사위원회": "40100010",
    "국민연금재심사위원회": "40100011",
}
_LCLSF, _MCLSF = "40150002", "40090007"
# 첨부 일련번호는 보통 1=HWP, 2=PDF지만 **뒤바뀐 건이 있다** (2026-09-12 실측: 68건 중 2건).
# 순서를 가정하지 말고 앞에서부터 받아 보고 %PDF로 시작하는 것을 쓴다.
_PDF_DTL_CANDIDATES = ("2", "1")


class HidrcError(Exception):
    pass


class HidrcUpstreamError(HidrcError):
    """네트워크·HTTP 오류 — 자료 부존재와 무관."""


class HidrcParseError(HidrcError):
    """응답 구조 변경 등으로 해석 실패 — 자료 부존재와 무관."""


def _csrf() -> str:
    """사이트 스크립트(com.generateCsrfToken)와 같은 방식 — 난수 32자의 base64."""
    return base64.b64encode(
        "".join(random.choices(string.ascii_letters + string.digits, k=32)).encode()).decode()


def _clean(s: str) -> str:
    s = (s or "").replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# 재결문 PDF의 머리글·쪽번호 — 문단 중간에 끼어들어 문장을 끊는다
_PDF_NOISE = re.compile(r"^\s*-\s*\d+\s*-\s*$|^<사건 제[^>]*>\s*-\s*\d+\s*-\s*$", re.M)


def pdf_to_text(blob: bytes) -> str:
    """재결문 PDF → 텍스트. pypdf는 적재 스크립트에서만 쓰므로 지연 임포트한다."""
    if not blob[:4] == b"%PDF":
        raise HidrcParseError("PDF가 아닌 응답 — 첨부 주소가 바뀌었거나 접근이 막혔습니다.")
    try:
        from pypdf import PdfReader
    except ImportError as e:                                   # noqa: BLE001
        raise HidrcError("pypdf가 필요합니다 — pip install pypdf") from e
    reader = PdfReader(io.BytesIO(blob))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    return _clean(_PDF_NOISE.sub("", text))


class HidrcClient:
    def __init__(self, min_interval: float = 0.4):
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": USER_AGENT, "Referer": SEED_URL})
        self._min_interval = max(0.0, float(min_interval))
        self._last = 0.0
        self._seeded = False

    def _throttle(self):
        el = time.time() - self._last
        if el < self._min_interval:
            time.sleep(self._min_interval - el)
        self._last = time.time()

    def _seed(self):
        """목록 화면을 한 번 열어 세션 쿠키를 받는다 — 없으면 AJAX가 예외를 돌려준다."""
        if self._seeded:
            return
        self._throttle()
        try:
            self._s.get(SEED_URL, timeout=TIMEOUT).raise_for_status()
        except requests.exceptions.RequestException as e:
            raise HidrcUpstreamError(
                f"simpan.go.kr 접속 실패: {type(e).__name__}: {e} — 자료 부존재와 무관합니다.") from e
        self._seeded = True

    def _post(self, url: str, payload: dict) -> dict:
        self._seed()
        self._throttle()
        try:
            r = self._s.post(f"{url}?cmnMenuCd={MENU}", json=payload, timeout=TIMEOUT,
                             headers={"X-CSRF-HEADER": _csrf(),
                                      "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            data = r.json()
        except ValueError as e:
            raise HidrcParseError("재결례 응답이 JSON이 아닙니다 (사이트 개편?)") from e
        except requests.exceptions.RequestException as e:
            raise HidrcUpstreamError(
                f"simpan.go.kr 요청 실패: {type(e).__name__}: {e} — 자료 부존재와 무관합니다.") from e
        if data.get("statusCode") != "S":
            raise HidrcUpstreamError(
                f"simpan.go.kr가 오류를 돌려줬습니다: {data.get('message') or data}")
        return data

    def _params(self, committee: str, page: int, keyword: str = "") -> dict:
        code = COMMITTEES.get(committee)
        if not code:
            raise ValueError(f"알 수 없는 위원회 {committee!r} — 사용 가능: {', '.join(COMMITTEES)}")
        return {"page": int(page), "srch_word": keyword or "", "def_srch_word": "",
                "redc_bgng_dt": "", "redc_end_dt": "",
                "cmt_lclsf_cd": _LCLSF, "cmt_mclsf_cd": _MCLSF, "cmt_sclsf_cd": code,
                "cmt_cd": "", "knwldg_clsf_systm_cd": "", "prcss_se_cd": "",
                "incdnt_no": "", "dspsofc_nm": "", "lwrg_nm": "", "redc_rslt_cd": ""}

    def list(self, page: int = 1, keyword: str = "",
             committee: str = "건강보험분쟁조정위원회") -> dict:
        """재결례 목록 1페이지(10건). 반환 {"total", "page", "items": [...]}"""
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")
        data = self._post(LIST_URL, self._params(committee, page, keyword))
        items = []
        for row in (data.get("list") or []):
            sn = row.get("pdf_doc_atch_file_sn") or row.get("korn_doc_atch_file_sn")
            items.append({
                "사건번호": _clean(row.get("incdnt_no")),
                "사건명": _clean(row.get("incdnt_nm") or row.get("doc_ttl")),
                "재결일자": _clean(row.get("redc_ymd")),
                "재결결과": _clean(row.get("adjdc_result_nm")),
                "위원회": _clean(row.get("cmt_nm")) or committee,
                "분류": _clean(row.get("knwldg_cl_1_nm")),
                "첨부일련번호": str(sn) if sn else "",
            })
        return {"total": int(data.get("totCnt") or 0), "page": page, "items": items}

    def iter_list(self, keyword: str = "", committee: str = "건강보험분쟁조정위원회",
                  max_pages: int = 0):
        """목록 전체를 순회한다. 건수가 68건뿐이라 페이지 부담이 없다."""
        page, total = 1, None
        while True:
            res = self.list(page=page, keyword=keyword, committee=committee)
            if total is None:
                total = res["total"]
            if not res["items"]:
                return
            for it in res["items"]:
                yield it
            if page * PAGE_SIZE >= total or (max_pages and page >= max_pages):
                return
            page += 1

    def _fetch_attachment(self, sn: str, dtl: str) -> bytes:
        info = self._post(DOWNLOAD_URL, {"atchFileSn": sn, "atchFileDtlSn": dtl})
        url = info.get("url")
        if not url:
            raise HidrcParseError(f"첨부 {sn}(-{dtl})의 내려받기 주소를 받지 못했습니다: {info}")
        self._throttle()
        try:
            r = self._s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise HidrcUpstreamError(
                f"재결문 내려받기 실패: {type(e).__name__}: {e} — 자료 부존재와 무관합니다.") from e
        return r.content

    def get_text(self, attach_sn: str) -> str:
        """첨부 재결문 PDF의 전문을 텍스트로. attach_sn은 목록의 '첨부일련번호'.

        첨부 번호와 형식의 짝이 건마다 다를 수 있어(HWP가 2번인 건이 있다) PDF가 나올
        때까지 후보를 훑는다. HWP만 있는 건은 읽지 못하므로 ParseError로 알린다.
        """
        sn = str(attach_sn or "").strip()
        if not sn.isdigit():
            raise ValueError(f"첨부일련번호는 숫자여야 합니다: {attach_sn!r}")
        last = None
        for dtl in _PDF_DTL_CANDIDATES:
            blob = self._fetch_attachment(sn, dtl)
            if blob[:4] == b"%PDF":
                return pdf_to_text(blob)
            last = blob[:8]
            logger.debug("첨부 %s-%s는 PDF가 아님(%r) — 다음 후보 시도", sn, dtl, last)
        raise HidrcParseError(
            f"첨부 {sn}에서 PDF를 찾지 못했습니다 (후보 {', '.join(_PDF_DTL_CANDIDATES)}, "
            f"마지막 응답 머리 {last!r}) — HWP만 올라온 건일 수 있습니다.")


# 재결문 본문에서 뽑아 쓸 표제 — 있으면 요지/본문을 나눠 담는다
_SECTIONS = ("재결요지", "주문", "청구취지", "이유")


def split_sections(text: str) -> dict:
    """재결문 텍스트를 표제별로 자른다. 못 자르면 {'전문': text}.

    PDF에서 뽑은 표제는 줄 첫머리에 오되 뒤에 내용이 바로 이어붙는다
    ("재결요지 국민건강보험법 제74조…") — 표제 뒤 줄바꿈을 요구하면 하나도 못 자른다.
    본문 안에서 같은 낱말이 다시 나올 수 있으므로 **각 표제의 첫 등장만** 쓰고,
    재결문의 정해진 차례(재결요지→주문→청구취지→이유)를 어기는 것은 버린다.
    """
    found = []
    for name in _SECTIONS:
        m = re.search(rf"(?:^|\n)[ \t]*(?:\[[ \t]*{name}[ \t]*\]|{name})[ \t]*", text)
        if m:
            found.append((_SECTIONS.index(name), m.start(), m.end(), name))
    if not found:
        return {"전문": text}
    marks, last_pos = [], -1
    for _, start, end, name in sorted(found):          # 정해진 차례대로 훑으며
        if start <= last_pos:                          # 앞 표제보다 먼저 나오면 오탐
            continue
        marks.append((start, end, name))
        last_pos = start
    out = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = _clean(text[end:stop])
        if body:
            out[name] = body
    if marks and marks[0][0] > 0:
        head = _clean(text[:marks[0][0]])
        if head:
            out["머리"] = head
    return out or {"전문": text}
