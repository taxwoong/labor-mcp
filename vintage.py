# -*- coding: utf-8 -*-
"""
vintage.py — 검색 결과의 '시점' 대조

노동법은 판례·행정해석 변경이 잦아, 오래된 사례가 **결론만 반대로 남아 있는** 일이 흔하다
(1년 기간제 연차 26일→11일, 통상임금 '고정성' 폐기, 주휴수당 '다음 주 근로 예정' 요건 폐지 등).
검색 결과에 그런 자료가 섞이면 그대로 인용해 틀린 답이 나간다.

이 모듈은 결과 목록을 훑어 ①시점 범위 ②일자 미상 건수 ③전환점보다 앞선 자료의 쟁점별 경고를
만든다. 아카이브 검색과 실시간 검색 도구가 같은 함수를 쓴다.

전환점 표는 labor_constants.DOCTRINE_TURNING_POINTS (resources/시행중_개정법_기준선.md 2절과 쌍).
"""
import re
from datetime import date as _date

import labor_constants as LC

_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")                    # 20211014
_DATE_SEP_RE = re.compile(r"^(\d{4})(?:\s*[-./년]\s*(\d{1,2})"              # 2016.5.9. / 2016-05-09
                          r"(?:\s*[-./월]\s*(\d{1,2}))?)?")                 # 연도만("2019")도 허용


def iso_date(value) -> str:
    """항목의 일자 표기에서 비교용 ISO 문자열을 뽑는다. 못 읽으면 ''.

    자료원마다 표기가 제각각이다 — '2016.5.9.'(노동위) · '2023.06.21'(위원회) ·
    '20211014'(법령 API) · '2019'(연도만). 월·일이 없으면 01로 채워 비교만 가능하게 한다.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    m = _DATE_COMPACT_RE.match(s) or _DATE_SEP_RE.match(s)
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2) or "1", m.group(3) or "1"
    try:
        mo_i, d_i = int(mo), int(d)
    except ValueError:
        return ""
    if not (1 <= mo_i <= 12 and 1 <= d_i <= 31):
        return ""
    return f"{y}-{mo_i:02d}-{d_i:02d}"


# 자료원마다 일자 필드 이름이 다르다 — 앞에 있는 것부터 찾아 쓴다.
# 새 원천을 붙일 때 그 원천의 일자 필드를 **여기 반드시 더할 것** — 빠뜨리면 일자가
# 멀쩡히 있는데도 '일자미상'으로 세어 엉뚱한 주의 문구가 붙는다
# (2026-09-12: 건강보험 재결례의 '재결일자'가 빠져 10건 전부 미상으로 집계됐다).
DATE_KEYS = ("일자_ISO", "일자", "선고일자", "해석일자", "판정일", "의결일자", "회신일자",
             "등록일", "발령일자", "시행일자", "처분일자", "재결일자", "결정년도")


def item_date(item: dict) -> str:
    for k in DATE_KEYS:
        v = iso_date(item.get(k))
        if v:
            return v
    return ""


def _haystack(item: dict) -> str:
    """쟁점 매칭에 쓸 텍스트 — 제목·요지·발췌·구분을 합친다."""
    keys = ("제목", "사건명", "안건명", "행정규칙명", "요지", "판정요지", "판정사항",
            "쟁점", "재결요지", "발췌", "구분", "문서번호")
    return " ".join(str(item.get(k, "")) for k in keys)


def annotate(items, keyword: str = "", today: str = "") -> dict:
    """결과 목록의 시점 정보와 경고를 만든다. 붙일 것이 없으면 빈 dict.

    items: 검색 결과 항목 리스트 (각 항목에 '일자' 또는 '일자_ISO')
    keyword: 사용자의 검색어 — 쟁점 매칭에 함께 쓴다
    """
    if not items:
        return {}
    today = today or _date.today().isoformat()
    dated, undated = [], 0
    for it in items:
        if not isinstance(it, dict):
            continue
        d = item_date(it)
        if d:
            dated.append((d, it))
        else:
            undated += 1

    out = {}
    if dated:
        ds = sorted(d for d, _ in dated)
        out["시점범위"] = ds[0] if ds[0] == ds[-1] else f"{ds[0]} ~ {ds[-1]}"
    if undated:
        out["일자미상"] = undated

    경고 = []
    kw = str(keyword or "")
    for 전환일, 쟁점, 주제어들, 부가어들, 설명 in LC.DOCTRINE_TURNING_POINTS:
        if 전환일 > today:                       # 아직 오지 않은 전환점은 대조 대상이 아니다
            continue
        검색어에_주제어 = any(k in kw for k in 주제어들)
        해당 = []
        for d, it in dated:
            if d >= 전환일:
                continue
            hay = kw + " " + _haystack(it)
            # 주제어 없이 부가어만 걸리면 오탐이다 ('동의'·'퇴직' 같은 흔한 낱말)
            if not any(k in hay for k in 주제어들):
                continue
            if 검색어에_주제어 or any(k in hay for k in 부가어들):
                해당.append(it)
        if 해당:
            경고.append({
                "쟁점": 쟁점, "전환일": 전환일, "해당건수": len(해당),
                "바뀐내용": 설명,
                "지시": f"위 결과 중 {len(해당)}건이 {전환일}(전환일)보다 앞선 자료입니다 — "
                       f"결론을 그대로 인용하지 말고 현행 기준으로 다시 판단하고, 답변에 "
                       f"자료의 일자를 함께 밝히세요.",
            })
    if 경고:
        out["시점주의"] = 경고

    # 전환점에 걸리지 않아도 오래된 자료면 일반 주의를 붙인다
    if dated and not 경고:
        cutoff = f"{int(today[:4]) - LC.STALE_YEARS}{today[4:]}"
        오래된 = sum(1 for d, _ in dated if d < cutoff)
        if 오래된:
            out["시점참고"] = (
                f"{오래된}건이 {LC.STALE_YEARS}년 이상 지난 자료입니다 — 그 사이 법령·판례가 "
                f"바뀌었을 수 있으니 labor_resource('table/시행중-개정법-기준선')로 현행 기준을 "
                f"대조하고, 답변에 자료의 일자를 함께 밝히세요.")
    if undated and "시점주의" not in out:
        out.setdefault("시점참고", "")
    if undated:
        미상안내 = (f"일자를 알 수 없는 자료 {undated}건이 포함됐습니다 — 원천이 일자를 제공하지 "
                 f"않는 자료(산재판례 등)이므로 현행성 판단에 특히 주의하세요.")
        out["시점참고"] = (out.get("시점참고") + " " + 미상안내).strip() if out.get("시점참고") else 미상안내
    return out


# 도구마다 결과 목록을 담는 키가 다르다
ITEM_KEYS = ("items", "cases", "고용노동부_행정해석", "법제처_법령해석례", "검색결과")


def apply_to(result: dict, items_key: str = "", keyword: str = "") -> dict:
    """검색 결과 dict에 시점 정보를 붙여 그대로 돌려준다.

    items_key를 주지 않으면 알려진 목록 키를 전부 모아 한 번에 판정한다 —
    행정해석+법령해석례처럼 목록이 둘인 응답도 함께 본다.
    """
    if not isinstance(result, dict):
        return result
    keys = (items_key,) if items_key else ITEM_KEYS
    items = []
    for k in keys:
        v = result.get(k)
        if isinstance(v, list):
            items.extend(x for x in v if isinstance(x, dict))
    info = annotate(items, keyword=keyword)
    if info:
        result.update(info)
    return result
