# -*- coding: utf-8 -*-
"""
nlrc.py — 중앙노동위원회(nlrc.go.kr) 판정·결정요지 검색 스크래퍼

공식 API가 없어 판정·결정요지 화면의 AJAX 엔드포인트를 그대로 사용한다
(2026-08-28 실호출 검증. 부당해고·부당노동행위 44,886건 등 총 6개 구분).

- 목록: POST /nlrc/mainCase/judgment/search/list.do
  파라미터: currentPage, pQuery(검색어 — 필수, 비우면 빈 응답), searchGubn[](구분 코드),
  key_end_fdate/key_end_tdate(판정일 범위, YYYY-MM-DD), key_comm_code(관할위원회).
  응답 HTML 목록 항목에 판정요지 전문이 인라인 포함된다.
- 상세: POST /nlrc/mainCase/judgment/search/detail.do
  목록 항목의 data-k1~k8 속성을 그대로 넘기면 판정사항·판정요지(교섭류는
  결정사항·결정요지) 표를 반환한다. 결정서(판정문) 전문은 제공되지 않는 화면이다.
- 로그인·세션 쿠키 모두 불필요함을 실측 확인 (쿠키 없는 첫 POST도 정상 응답).
  단 장애 시 자동 복구를 위해 세션 부트스트랩·재획득 로직은 유지한다.

실측 제약 (2026-08-28):
- www.nlrc.go.kr은 POST를 302로 nlrc.go.kr에 넘기면서 본문을 유실시킨다(재요청이
  GET으로 바뀜) — 반드시 https://nlrc.go.kr 호스트로 직접 요청해야 한다.
- 구분(searchGubn[])을 1개만 지정하면 페이지당 10건 + 페이지네이션이 동작하고,
  2개 이상(또는 전체)이면 구분별 미리보기 약 5건만 반환되며 currentPage는 무시된다.
- 판정요지 텍스트에는 검색어 하이라이트가 이스케이프된 '<b>…</b>' 문자열로 섞여
  들어온다(서버가 &lt;b&gt;로 내려주고 화면 JS가 재해석하는 구조) — 파싱 시 제거.
"""

import logging
import re
import time
from typing import Optional, Union

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("nlrc")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# www 호스트는 POST 본문을 유실시키는 302를 반환하므로 절대 사용 금지 (모듈 docstring 참조)
BASE_URL = "https://nlrc.go.kr"
INDEX_URL = f"{BASE_URL}/nlrc/mainCase/judgment/search/index.do"
LIST_URL = f"{BASE_URL}/nlrc/mainCase/judgment/search/list.do"
DETAIL_URL = f"{BASE_URL}/nlrc/mainCase/judgment/search/detail.do"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 구분 코드 (research/nlrc.html 검색폼 체크박스 실측 — 추측 아님)
CATEGORIES = {
    "부당해고": "BH",         # 부당해고·부당노동행위
    "차별시정": "DR",
    "교섭창구단일화": "RP",   # 교섭창구 단일화 절차
    "교섭단위": "SP",         # 교섭단위 결정
    "공정대표": "GJ",         # 공정대표 의무위반
    "기타": "ET",             # 기타판정
}
CATEGORY_NAME_BY_CODE = {
    "BH": "부당해고·부당노동행위",
    "DR": "차별시정",
    "RP": "교섭창구 단일화 절차",   # 목록 응답 헤더는 '교섭대표 단일화 절차'로 표기됨 (사이트 자체 불일치)
    "SP": "교섭단위 결정",
    "GJ": "공정대표 의무위반",
    "ET": "기타판정",
}

# 관할위원회 코드 (research/nlrc.html select 옵션 실측. '91'은 연습용이라 제외)
COMMITTEES = {
    "전체": "all",
    "중앙": "00", "서울": "01", "부산": "02", "경기": "03", "충남": "04",
    "전남": "05", "경북": "06", "경남": "07", "인천": "08", "강원": "09",
    "충북": "10", "전북": "11", "제주": "12", "울산": "13",
}

PAGE_SIZE = 10           # 단일 구분 검색의 서버 고정 페이지 크기 (실측)
DEFAULT_CACHE_TTL = 300
PAGE_SIZE = 10          # 단일 구분 검색의 페이지당 건수 (실측)

# 필수 필드가 이 비율 이상 비면 파서가 깨진 것으로 본다. 전면 붕괴(카테고리 0개)만
# 잡는 게이트로는 부족하다 — 셀렉터 하나만 바뀌면 items 개수는 정상이고 내용만 빈값이 되어
# "정상 결과"로 보이기 때문이다 (2026-09-01 리뷰).
FIELD_BLANK_THRESHOLD = 0.5


class NlrcParseError(RuntimeError):
    """응답은 받았으나 구조가 예상과 달라 결과를 읽지 못함 — 자료 부존재와 무관."""


class NlrcUpstreamError(RuntimeError):
    """노동위원회 사이트 접속·세션 실패 — 자료 부존재와 무관."""
DEFAULT_MIN_INTERVAL = 0.5

_HIGHLIGHT_RE = re.compile(r"</?b>")
_WS_RE = re.compile(r"\s+")


class NlrcError(Exception):
    pass


def _clean_text(s: str) -> str:
    """하이라이트 마커('<b>' 문자열) 제거 + 공백 정규화."""
    return _WS_RE.sub(" ", _HIGHLIGHT_RE.sub("", s or "")).strip()


def _normalize_date(d: str) -> str:
    """YYYYMMDD / YYYY.MM.DD / YYYY-MM-DD → 서버 형식 YYYY-MM-DD ('' 은 그대로)."""
    if not d:
        return ""
    digits = re.sub(r"\D", "", d)
    if len(digits) != 8:
        raise ValueError(f"날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다: {d!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _resolve_categories(categories) -> list:
    """사람이 읽는 키('부당해고' 등)/코드('BH' 등) 혼용 입력 → 코드 리스트."""
    if categories is None:
        return list(CATEGORY_NAME_BY_CODE.keys())
    if isinstance(categories, str):
        categories = [categories]
    codes = []
    for c in categories:
        c = c.strip()
        if c in CATEGORY_NAME_BY_CODE:
            codes.append(c)
        elif c in CATEGORIES:
            codes.append(CATEGORIES[c])
        else:
            raise ValueError(
                f"알 수 없는 카테고리: {c!r} — 사용 가능: {list(CATEGORIES.keys())} 또는 코드 {list(CATEGORY_NAME_BY_CODE.keys())}")
    return codes


def _resolve_committee(committee: str) -> str:
    """위원회명(부분 문자열 허용: '중앙', '서울지방노동위원회' 등) 또는 코드 → 코드."""
    if not committee or committee == "all":
        return "all"
    if committee in COMMITTEES.values():
        return committee
    for name, code in COMMITTEES.items():
        if name in committee:
            return code
    raise ValueError(
        f"알 수 없는 관할위원회: {committee!r} — 사용 가능: {list(COMMITTEES.keys())} 또는 코드 {sorted(COMMITTEES.values())}")


def parse_list_html(html: str) -> dict:
    """list.do 응답 HTML → {"categories": [{구분, total, items[]}], "total_pages": int|None}

    항목 구조 (research/nlrc_list.html 실측):
      div.C_body > ul.Cmenu_Title(구분명·총건수) + dl.C_Cts*
      dl.C_Cts > dt.tit > a[data-event=click-detail][data-k1~k8]
        > strong(위원회) + span(사건번호) + span(사건명), em.date(판정일), em.date(|결과|)
      dl.C_Cts > dd.txt(판정요지 — &lt;b&gt; 이스케이프 하이라이트 포함)
    """
    soup = BeautifulSoup(html, "html.parser")
    categories = []
    for body in soup.select("div.C_body"):
        title_ul = body.select_one("ul.Cmenu_Title")
        if title_ul is None:
            continue
        lis = title_ul.find_all("li")
        name = lis[0].get_text(strip=True) if lis else ""
        total = None
        if len(lis) > 1:
            m = re.search(r"([\d,]+)", lis[1].get_text(strip=True))
            if m:
                total = int(m.group(1).replace(",", ""))

        items = []
        for dl in body.select("dl.C_Cts"):
            item = _parse_item(dl, name)
            if item:
                items.append(item)
        categories.append({"구분": name, "total": total, "items": items})

    # 단일 구분 검색에서만 페이지네이션 앵커가 내려온다.
    # 마지막 앵커('끝' 버튼)가 최종 페이지 번호 (실측: 44,886건 → 4489)
    page_indexes = [
        int(a.get("data-pageindex"))
        for a in soup.select("[data-pageindex]")
        if str(a.get("data-pageindex", "")).isdigit()
    ]
    total_pages = max(page_indexes) if page_indexes else None

    return {"categories": categories, "total_pages": total_pages}


def _parse_item(dl, category_name: str) -> Optional[dict]:
    a = dl.select_one('a[data-event="click-detail"]')
    if a is None:
        return None

    # get_text()를 구분자 없이 써야 '부당<b>해고</b>'가 '부당 해고'로 벌어지지 않는다
    # (하이라이트 <b>가 사건명·사건번호 span 안에 실태그로 들어옴)
    strong = a.find("strong")
    spans = a.find_all("span")
    committee = strong.get_text(strip=True) if strong else ""
    case_no = _clean_text(spans[0].get_text()) if spans else ""
    case_title = _clean_text(spans[1].get_text()) if len(spans) > 1 else ""

    # em.date 첫 번째 = 판정일, 두 번째 = '\xa0|\xa0기각\xa0|\xa0' 형태의 결과
    dates = dl.select("em.date")
    decision_date = dates[0].get_text(strip=True) if dates else ""
    result = ""
    if len(dates) > 1:
        result = dates[1].get_text(strip=True).replace("\xa0", " ").strip(" |")

    dd = dl.select_one("dd.txt")
    summary = _clean_text(dd.get_text()) if dd else ""

    detail_keys = {f"k{i}": a.get(f"data-k{i}") or "" for i in range(1, 9)}
    detail_keys["table_nm"] = a.get("data-table-nm") or ""

    return {
        "구분": category_name,
        "위원회": committee,
        "사건번호": case_no,
        "사건명": case_title,
        "판정일": decision_date,
        "결과": result,
        "판정요지": summary,
        "detail_keys": detail_keys,
    }


def _detail_type(even_gubn: str, midd_rscd: str) -> tuple:
    """(type, subType) 결정 — 검색 페이지 JS의 switch 로직 이식 (research/nlrc.html).

    ME(조정)·AR(중재)에서 midd_rscd 11/50(취하)은 결정서 자료가 없다고 안내되는 케이스.
    AS(중재재심)는 원본 JS에 break 누락으로 NR30 케이스로 흘러 brjuPoin/05가 최종값이
    되므로 그 실효 동작을 따른다. 판정요지 검색 목록의 실측 even_gubn은
    JR*/JS*/DS/NR*/NS* 계열이라 대부분 brjuPoin(판정요지) 경로를 탄다.
    """
    if even_gubn == "ME":
        if midd_rscd in ("10", "20"):
            return "mediPlan", "01"
        if midd_rscd in ("30", "40"):
            return "defnShet", "02"
        raise NlrcError("사건결과가 [취하]인 경우는 결정서 자료가 없습니다.")
    if even_gubn == "AR":
        if midd_rscd in ("10", "20"):
            return "defnShet", "03"
        raise NlrcError("사건결과가 [취하]인 경우는 결정서 자료가 없습니다.")
    if even_gubn == "AS":
        if midd_rscd in ("10", "20", "30"):
            return "brjuPoin", "05"
        raise NlrcError("사건결과가 [취하]인 경우는 결정서 자료가 없습니다.")
    if even_gubn in ("NR30", "NR31", "NS30", "NS31"):
        return "brjuPoin", "05"
    return "brjuPoin", "06"


def parse_detail_html(html: str) -> dict:
    """detail.do 응답 HTML → {"found", "위원회", "사건번호", "sections": {제목: 본문}}

    표 구조 (실측): th "{위원회}\n판정요지" → td "사건 : {사건번호}" →
    (th 섹션제목 → td>pre 본문)* — 교섭류 사건은 '결정사항'·'결정요지' 라벨.
    잘못된 키로 조회해도 200 + 빈 표가 오므로 사건번호·본문 유무로 found를 판정한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div.BD_table table")
    if table is None:
        return {"found": False, "message": "상세 표를 찾지 못했습니다 (화면 개편 가능성).",
                "위원회": "", "사건번호": "", "sections": {}}

    committee, case_no = "", ""
    sections = {}
    pending_title = None
    for cell in table.find_all(["th", "td"]):
        text = cell.get_text(" ", strip=True)
        if cell.name == "th":
            if not committee:
                # 첫 th = "{위원회} 판정요지" 또는 "{위원회} 결정요지"
                committee = re.sub(r"(판정요지|결정요지)\s*$", "", text).strip()
                pending_title = None
            else:
                pending_title = text
        else:
            m = re.match(r"^사건\s*[::]\s*(.*)$", text)
            if m:
                case_no = m.group(1).strip()
            elif pending_title:
                # <pre> 안의 본문은 개행이 의미를 가지므로 원형 유지
                pre = cell.find("pre")
                content = (pre.get_text("\n") if pre else cell.get_text("\n")).strip()
                if content:
                    sections[pending_title] = content
                pending_title = None

    found = bool(case_no) and bool(sections)
    result = {"found": found, "위원회": committee, "사건번호": case_no, "sections": sections}
    if not found:
        result["message"] = "해당 키로 조회된 판정요지가 없습니다 (detail_keys 확인)."
    return result


class NlrcClient:
    """노동위원회 판정·결정요지 검색 클라이언트 (요청 간격 제한 + 캐싱 + 세션 재획득)"""

    def __init__(
        self,
        verify_ssl: bool = True,
        timeout: int = 20,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        min_request_interval: float = DEFAULT_MIN_INTERVAL,
    ):
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._cache_ttl = cache_ttl
        self._cache: dict = {}
        self._min_request_interval = min_request_interval
        self._last_request_ts = 0.0
        self._bootstrapped = False
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        return s

    def _bootstrap(self, force: bool = False):
        """검색 페이지 1회 GET으로 세션 쿠키 확보. 실측상 쿠키 없이도 동작하지만
        차단·장애 시 새 세션으로 갈아타는 복구 경로를 겸한다."""
        if force:
            self.session = self._new_session()
            self._bootstrapped = False
        resp = self.session.get(INDEX_URL, verify=self.verify_ssl, timeout=self.timeout)
        resp.raise_for_status()
        self._bootstrapped = True
        logger.info("nlrc 세션 부트스트랩 완료 (force=%s)", force)

    def _throttle(self):
        elapsed = time.time() - self._last_request_ts
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_ts = time.time()

    def _cache_key(self, **kwargs) -> str:
        import json
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False)

    def _post(self, url: str, data, validate) -> str:
        """POST + 응답 검증. validate(text)가 False면 세션 재획득 후 1회 재시도."""
        if not self._bootstrapped:
            self._bootstrap()

        def _do():
            self._throttle()
            resp = self.session.post(
                url, data=data,
                headers={"Referer": INDEX_URL, "X-Requested-With": "XMLHttpRequest"},
                verify=self.verify_ssl, timeout=self.timeout,
            )
            resp.raise_for_status()
            resp.encoding = "utf-8"
            if not validate(resp.text):
                raise ValueError("예상한 응답 형태가 아닙니다 (세션 만료·차단·파라미터 유실 가능성)")
            return resp.text

        try:
            return _do()
        except (ValueError, requests.exceptions.RequestException) as e:
            logger.warning("nlrc 요청 실패, 세션 재획득 후 재시도: %s", e)
            self._bootstrap(force=True)
            return _do()

    def search(
        self,
        keyword: str,
        categories: Optional[Union[str, list]] = None,
        page: int = 1,
        date_from: str = "",
        date_to: str = "",
        committee: str = "",
        use_cache: bool = True,
    ) -> dict:
        """판정·결정요지 목록 검색. 판정요지 전문이 목록에 인라인 포함된다.

        Args:
            keyword: 검색어 (필수 — 서버가 빈 검색어에는 빈 응답을 반환).
                특수문자는 화면 JS가 차단하는 규칙이 있어 미리 제거한다.
            categories: 구분. "부당해고"/"차별시정"/"교섭창구단일화"/"교섭단위"/
                "공정대표"/"기타" 또는 코드(BH/DR/RP/SP/GJ/ET). 문자열 1개나 리스트.
                None이면 전체 6개.
                ※ 1개만 지정하면 페이지당 10건 + page 파라미터 동작(서버측),
                  2개 이상이면 구분별 미리보기 약 5건만 오고 page는 무시된다(실측).
            page: 페이지 번호(1부터). 단일 구분 검색에서만 유효.
            date_from / date_to: 판정일 범위 (YYYYMMDD 또는 YYYY-MM-DD). 서버측 필터.
            committee: 관할위원회 ("중앙", "서울" 등 이름 일부 또는 코드 "00"~"13").
                빈 값이면 전체.
            use_cache: 동일 파라미터 5분 캐시 사용 여부.

        Returns:
            {"items": [{구분, 위원회, 사건번호, 사건명, 판정일, 결과, 판정요지,
                        detail_keys(k1~k8·table_nm — get_detail 인자)}],
             "total": int|None (단일 구분=해당 구분 총건수, 복수=합계),
             "totals_by_category": {구분명: 총건수},
             "page": int, "total_pages": int|None (단일 구분에서만),
             "notice": str (있을 때만)}
        """
        _raw_keyword = (keyword or "").strip()
        keyword = re.sub(r"[{}\[\]/?.,;:|)*~!`^\-_+<>@#$%&\\=('\"]", " ", keyword or "").strip()
        if not keyword:
            raise ValueError("keyword는 필수입니다 — nlrc 검색은 빈 검색어를 지원하지 않습니다 (서버측 제약).")

        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(f"page는 1 이상의 정수여야 합니다: {page!r}")

        codes = _resolve_categories(categories)
        comm_code = _resolve_committee(committee)
        fdate, tdate = _normalize_date(date_from), _normalize_date(date_to)
        if fdate and tdate and fdate > tdate:
            raise ValueError(f"판정일 범위가 뒤집혔습니다: {fdate} > {tdate}")

        cache_key = None
        if use_cache:
            cache_key = self._cache_key(kw=keyword, codes=codes, page=page,
                                        fdate=fdate, tdate=tdate, comm=comm_code)
            hit = self._cache.get(cache_key)
            if hit and (time.time() - hit[0]) < self._cache_ttl:
                logger.info("캐시된 결과 반환: %s", keyword)
                return hit[1]

        data = [
            ("currentPage", str(page)),
            ("pQuery", keyword),
            ("key_end_fdate", fdate),
            ("key_end_tdate", tdate),
            ("key_comm_code", comm_code),
        ] + [("searchGubn[]", c) for c in codes]

        # 정상 응답은 검색어를 inputChk로 그대로 되돌려준다(0건이어도) — 빈 echo는
        # 파라미터 유실(www 리다이렉트·세션 문제) 신호이므로 재시도 대상 (실측 확인)
        html = self._post(
            LIST_URL, data,
            validate=lambda t: 'id="searchForm"' in t
            and 'name="inputChk" value=""' not in t)
        parsed = parse_list_html(html)

        items = []
        totals_by_category = {}
        for cat in parsed["categories"]:
            totals_by_category[cat["구분"]] = cat["total"]
            items.extend(cat["items"])

        known_totals = [t for t in totals_by_category.values() if t is not None]
        total = sum(known_totals) if known_totals else None

        # --- 파서 생존 판정 3단 게이트 ---------------------------------------
        # 진짜 0건일 때도 카테고리 헤더와 total은 반드시 내려온다(실측). 따라서 추가
        # 네트워크 왕복(카나리) 없이 "결과 없음"과 "파서 파손"을 구조적으로 구분할 수 있다.
        _behavior = ("사용자에게 조회 실패를 알리고, 판정례가 없다고 단정하지 마세요. "
                     "필요하면 nlrc.go.kr에서 직접 확인하도록 안내하세요.")
        if not parsed["categories"]:
            raise NlrcParseError(
                "노동위원회 사이트 구조가 바뀌어 결과 영역을 읽지 못했습니다 (카테고리 0개). "
                "검색 결과가 없다는 뜻이 아닙니다. " + _behavior)
        if total and total > 0 and not items:
            raise NlrcParseError(
                f"총 {total}건이 있다고 나오는데 목록을 한 건도 읽지 못했습니다 — "
                "결과 항목의 마크업이 바뀐 것으로 보입니다. 자료 부존재가 아닙니다. " + _behavior)
        if items:
            blank = sum(1 for it in items if not it.get("사건번호") or not it.get("판정일"))
            if blank / len(items) >= FIELD_BLANK_THRESHOLD:
                raise NlrcParseError(
                    f"목록 {len(items)}건 중 {blank}건에서 사건번호·판정일을 읽지 못했습니다 — "
                    "항목 내부 마크업이 바뀐 것으로 보입니다. 자료 부존재가 아닙니다. " + _behavior)
        # ---------------------------------------------------------------------

        # 페이지네이션 앵커는 요청 윈도 주변만 내려와 범위를 벗어나면 오염된다
        # (실측: page=99999 → total_pages 99990). 총건수로 계산한 값을 우선한다.
        if len(codes) == 1 and total is not None:
            parsed["total_pages"] = max(1, -(-total // PAGE_SIZE))

        result = {
            "items": items,
            "total": total,
            "totals_by_category": totals_by_category,
            "page": page,
            "total_pages": parsed["total_pages"],
        }
        notices = []
        if re.sub(r"\s+", " ", _raw_keyword) != re.sub(r"\s+", " ", keyword):
            result["정규화된_검색어"] = keyword
            notices.append(
                f"검색어의 특수문자를 공백으로 바꿔 '{keyword}'로 조회했습니다 "
                f"(원본 '{_raw_keyword}'). 문서번호·사건번호로 찾는 중이라면 결과가 "
                "다를 수 있습니다.")
        if len(codes) > 1 or categories is None:
            notices.append(
                "구분을 2개 이상(또는 전체) 지정하면 서버가 구분별 미리보기 약 5건만 "
                "반환하며 page가 무시됩니다. 더 보려면 categories를 1개로 좁혀 주세요.")
        if (parsed["total_pages"] or 1) < page and total:
            notices.append(
                f"요청한 page={page}가 범위를 벗어났습니다 (총 {total}건 / "
                f"{parsed['total_pages']}페이지). 결과가 없다는 뜻이 아닙니다 — "
                "page를 범위 안으로 낮춰 다시 조회하세요.")
        elif total == 0 or not items:
            notices.append(
                f"'{keyword}' 검색 결과가 없습니다 (검색은 정상 수행됐고 총건수 0건으로 "
                "확인됨). 검색어를 짧게 바꾸거나 다른 구분·기간으로 다시 시도해 보세요.")
        if notices:
            result["notice"] = " / ".join(notices)

        if use_cache:
            self._cache[cache_key] = (time.time(), result)
        return result

    def get_detail(self, detail_keys: dict, use_cache: bool = True) -> dict:
        """판정요지 상세 조회 — search() 결과 항목의 detail_keys를 그대로 넘긴다.

        상세 화면은 판정사항·판정요지(교섭류는 결정사항·결정요지) 2개 섹션 표이며,
        결정서(판정문) 전문은 이 화면에서 제공되지 않는다.

        Args:
            detail_keys: {"k1"~"k8", ...} — k1=위원회코드, k2=사건키, k3=사건구분,
                k4=재심여부, k5=결과코드, k6/k7=병합여부, k8=연계사건번호.

        Returns:
            {"found": bool, "위원회": str, "사건번호": str,
             "sections": {"판정사항": str, "판정요지": str, ...},
             "message": str (found=False일 때)}
        """
        k = {f"k{i}": (detail_keys.get(f"k{i}") or "") for i in range(1, 9)}
        if not k["k2"]:
            raise ValueError("detail_keys에 k2(사건키)가 없습니다 — search() 결과의 detail_keys를 그대로 전달하세요.")

        try:
            type_, sub_type = _detail_type(k["k3"], k["k5"])
        except NlrcError as e:
            return {"found": False, "message": str(e), "위원회": "", "사건번호": "", "sections": {}}

        cache_key = None
        if use_cache:
            cache_key = self._cache_key(detail=sorted(k.items()))
            hit = self._cache.get(cache_key)
            if hit and (time.time() - hit[0]) < self._cache_ttl:
                return hit[1]

        data = {
            "type": type_,
            "subType": sub_type,
            "even_gubn": k["k3"],
            "comm_code": k["k1"],
            "even_numb": k["k2"],
            "resu_yeno": k["k4"],
            "midd_rscd": k["k5"],
            "r_merge_yesno": k["k6"],
            "r_rep_merge_yesno": k["k7"],
            "r_link_even_numb": k["k8"],
            "detail_gubn": "",
        }
        html = self._post(DETAIL_URL, data, validate=lambda t: "BD_table" in t)
        result = parse_detail_html(html)

        if use_cache:
            self._cache[cache_key] = (time.time(), result)
        return result


if __name__ == "__main__":
    client = NlrcClient()

    print("=== '해고' 부당해고·부당노동행위 검색 (1페이지) ===")
    res = client.search("해고", categories=["부당해고"], page=1)
    print(f"총 {res['total']:,}건 / {res['total_pages']:,}페이지")
    for it in res["items"][:5]:
        print(f"- {it['위원회']} {it['사건번호']} ({it['판정일']}, {it['결과']}) {it['사건명']}")
        print(f"  요지: {it['판정요지'][:80]}…")

    print("\n=== 첫 건 상세 ===")
    detail = client.get_detail(res["items"][0]["detail_keys"])
    print("found:", detail["found"], "/", detail["위원회"], detail["사건번호"])
    for title, body in detail["sections"].items():
        print(f"[{title}] {body[:100]}…")
