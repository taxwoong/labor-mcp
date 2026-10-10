# -*- coding: utf-8 -*-
"""
test_patch_b.py — 패치 B 오프라인 테스트: 조문 모드 NOT_FOUND 안내, 기관코드(OC) 가림(도구 응답·로그·적재 로그),
부칙 모드 법령명 정확 일치, 서버 안내문(시행중-개정법-기준선 문구).

law.go.kr에는 접속하지 않는다 (requests.get·_get을 가짜로 바꿔 끼움). OC도 가짜 값.

실행:
    python tests/test_patch_b.py
    python -m pytest tests/test_patch_b.py -q
"""
import io
import logging
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import law_go_kr  # noqa: E402
from law_go_kr import LawInvalidInput, LawNotFound, LawUpstreamError  # noqa: E402

FAKE_OC = "DUMMYOC123"


class _FakeOC:
    def __enter__(self):
        self.orig = law_go_kr.OC
        law_go_kr.OC = FAKE_OC
        return self

    def __exit__(self, *exc):
        law_go_kr.OC = self.orig
        return False


def _resp(status, url):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = b"<html>error</html>"
    return r


def test_get_wraps_masked_upstream_error_and_keeps_http_cause():
    """HTTP 500 — LawUpstreamError 메시지에 OC가 없고, 폴백 판정용 __cause__(HTTPError, 상태코드)는 남는다
    (2026-10-09 리뷰 재현: 가짜 OC와 닫힌 포트로 law_article_as_of → 메시지에 'OC=<값>')."""
    orig = law_go_kr.requests.get
    law_go_kr.requests.get = lambda url, params=None, **kw: _resp(500, f"{url}?OC={params['OC']}&type=XML")
    try:
        with _FakeOC():
            try:
                law_go_kr._get("lawService.do", target="law", MST="1")
                raise AssertionError("LawUpstreamError가 나야 함")
            except LawUpstreamError as e:
                assert FAKE_OC not in str(e) and "OC=***" in str(e), str(e)
                cause = e.__cause__
                assert isinstance(cause, requests.exceptions.HTTPError) and cause.response.status_code == 500
                assert FAKE_OC not in str(cause), str(cause)
    finally:
        law_go_kr.requests.get = orig

    def boom(url, params=None, **kw):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: /DRF/lawSearch.do?OC={params['OC']}")
    law_go_kr.requests.get = boom
    try:
        with _FakeOC():
            try:
                law_go_kr._get("lawSearch.do", target="law", query="근로기준법")
                raise AssertionError("LawUpstreamError가 나야 함")
            except LawUpstreamError as e:
                assert FAKE_OC not in str(e) and FAKE_OC not in str(e.__cause__), str(e)
    finally:
        law_go_kr.requests.get = orig


def test_guard_masks_error_field_and_log():
    import server as S
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(h)
    law_go_kr.install_oc_log_mask()
    try:
        @S._guard()
        def tool():
            raise LawUpstreamError(f"law.go.kr 접속 실패: url ?OC={FAKE_OC}&type=XML")
        with _FakeOC():
            out = tool()
        assert out["status"] == "UPSTREAM_ERROR" and FAKE_OC not in out["오류"], out
        assert FAKE_OC not in buf.getvalue(), buf.getvalue()
    finally:
        root.removeHandler(h)


def test_ingest_log_path_masks_oc():
    """아카이브 적재(ingest_archive) 로그도 가린다 — 재시도 경고·실패 트레이스백."""
    import ingest_archive as IA
    buf = io.StringIO()
    lg = logging.getLogger("ingest")
    h = logging.StreamHandler(buf)
    lg.addHandler(h)
    law_go_kr.install_oc_log_mask(lg)
    try:
        with _FakeOC():
            r = IA.Runner.__new__(IA.Runner)

            class _T:
                def wait(self):
                    pass
            r.throttle = _T()
            orig_sleep = IA.time.sleep
            IA.time.sleep = lambda s: None
            try:
                def fn():
                    raise LawUpstreamError(f"law.go.kr 접속 실패: HTTPError: 500 for url: ?OC={FAKE_OC}&x=1")
                try:
                    r._call(fn, "시험", tries=2)
                except LawUpstreamError:
                    pass
                try:
                    raise LawUpstreamError(f"마지막 실패 OC={FAKE_OC}")
                except LawUpstreamError as e:
                    lg.exception("=== %s 실패: %s", "prec", e)
            finally:
                IA.time.sleep = orig_sleep
        text = buf.getvalue()
        assert "일시 오류" in text and FAKE_OC not in text, text
    finally:
        lg.removeHandler(h)


def test_article_mode_not_found_guidance():
    """조문 모드 NOT_FOUND는 '단정하지 말고 mode=연혁 확인' 안내 — 다른 도구 NOT_FOUND 안내는 그대로."""
    import server as S
    orig = S._law.law_article_as_of
    S._law.law_article_as_of = lambda *a, **k: (_ for _ in ()).throw(
        LawNotFound("20261009 당시 시행본(20261008 시행 통합본)에 제44조의4 조문이 없음"))
    try:
        out = S.labor_law_article(law="근로기준법", article_no="44의4")
    finally:
        S._law.law_article_as_of = orig
    assert out["status"] == "NOT_FOUND" and "단정하지 말고" in out["안내"] and "mode='연혁'" in out["안내"], out
    assert "단정하지 말고" not in S._GUIDANCE["NOT_FOUND"]


def _search_xml(names):
    return "<LawSearch>" + "".join(
        f"<law id='{i}'><법령명한글><![CDATA[{n}]]></법령명한글><법령ID>{900000 + i}</법령ID>"
        f"<법령일련번호>{1000 + i}</법령일련번호><시행일자>20260910</시행일자><공포일자>20260310</공포일자>"
        f"<공포번호>{i}</공포번호><제개정구분명>일부개정</제개정구분명></law>"
        for i, n in enumerate(names, 1)) + "</LawSearch>"


def test_addenda_name_must_match_exactly():
    """부칙 모드도 이름이 정확히 같은 현행 법령만 — 예전에는 첫 검색 결과로 내려가 '고용보험' 부칙이 보험료징수법
    부칙으로 OK가 됐다. 정확 일치가 없으면 후보와 함께 INVALID_INPUT."""
    names = ["고용보험법", "고용보험법 시행령", "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률"]
    calls = []

    def fake(endpoint, **params):
        calls.append((endpoint, params.get("target"), params.get("display")))
        if endpoint == "lawSearch.do" and params.get("target") == "law":
            return _search_xml(names)
        return "<LawSearch></LawSearch>"
    orig = law_go_kr._get
    law_go_kr._get = fake
    try:
        c = law_go_kr.LawGoKrClient()
        try:
            c.law_addenda(law_name="고용보험", recent=1)
            raise AssertionError("INVALID_INPUT이어야 함")
        except LawInvalidInput as e:
            assert "고용보험법" in str(e) and "이름이 정확히 같은" in str(e), e
    finally:
        law_go_kr._get = orig
    assert ("lawSearch.do", "law", 100) in calls, calls


def test_instructions_and_baseline_wording():
    import server as S
    text = S.mcp.instructions
    assert "2026-08-28 기준 요약 — 이후 개정은 labor_law_article(mode=연혁)로 확인" in text
    for phrase in ("labor_law_article(as_of_date 생략 = 오늘)", "적용시행본", "mode='부칙'(article_no)",
                   "시행예정_부분", "국회 계류", "조문 모드의 NOT_FOUND는"):
        assert phrase in text, phrase


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, f"{type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
