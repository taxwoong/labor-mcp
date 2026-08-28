"""
test_mcp_client.py

Claude 커넥터를 거치지 않고, labor-mcp 서버에 직접 요청을 보내서
서버 자체가 정상 동작하는지 확인하는 독립 테스트 스크립트입니다.

Claude 쪽 채팅에서 "도구가 안 잡힌다"는 문제가 생겼을 때,
- 이 스크립트가 성공하면 -> 서버는 정상. 문제는 Claude 커넥터 인식/캐싱 쪽.
- 이 스크립트도 실패하면 -> 서버 자체 문제.
로 원인을 빠르게 나눠볼 수 있습니다.

사용법:
    python test_mcp_client.py                                   (로컬 8735)
    python test_mcp_client.py --url https://<호스트>.ts.net/<비밀경로>/mcp
"""

import argparse
import json
import sys

import requests

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# tools/list에 반드시 있어야 하는 도구 (server.py의 15종 중 핵심)
EXPECTED_TOOLS = {
    "labor_law_article", "moel_interpretation_search", "labor_case_search",
    "nlrc_decision_search", "check_minimum_wage", "analyze_payroll",
    "design_pay_table", "labor_resource",
}


def _parse_sse(text: str) -> dict:
    """서버가 text/event-stream(SSE) 형식으로 응답하므로 'data: {...}' 줄만 파싱합니다."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(text)


def call(url: str, payload: dict, session_id: str = None) -> tuple:
    headers = dict(HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    if not resp.text.strip():  # notifications는 202 + 빈 본문으로 응답한다
        return {}, resp.headers.get("Mcp-Session-Id")
    return _parse_sse(resp.text), resp.headers.get("Mcp-Session-Id")


def tool_result(result: dict) -> dict:
    return json.loads(result["result"]["content"][0]["text"])


def run(url: str) -> bool:
    ok = True
    print(f"대상 서버: {url}\n")

    # 1) initialize — stateful 서버이므로 발급된 세션 ID를 이후 요청에 실어야 한다
    print("[1/5] initialize 핸드셰이크...")
    try:
        result, session_id = call(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        })
        server_info = result.get("result", {}).get("serverInfo", {})
        print(f"    성공 — 서버 이름: {server_info.get('name')}, 세션: {'발급됨' if session_id else '없음(stateless)'}")
        call(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
    except Exception as e:
        print(f"    실패: {e}")
        return False

    # 2) tools/list
    print("[2/5] tools/list 조회...")
    try:
        result, _ = call(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id)
        names = {t["name"] for t in result.get("result", {}).get("tools", [])}
        missing = EXPECTED_TOOLS - names
        print(f"    성공 — 도구 {len(names)}종: {sorted(names)}")
        if missing:
            print(f"    경고: 빠진 도구 {sorted(missing)}")
            ok = False
    except Exception as e:
        print(f"    실패: {e}")
        return False

    # 3) tools/call - labor_resource 목록 (네트워크 불필요 — 서버 내부 리소스)
    print("[3/5] labor_resource 목록 조회...")
    try:
        result, _ = call(url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "labor_resource", "arguments": {}},
        }, session_id)
        parsed = tool_result(result)
        n = len(parsed.get("리소스목록", []))
        print(f"    성공 — 리소스 {n}종")
        if n < 11:
            print("    경고: 리소스가 11종 미만입니다.")
            ok = False
    except Exception as e:
        print(f"    실패: {e}")
        ok = False

    # 4) tools/call - check_minimum_wage (격월 상여 불산입 → 위반 검출 케이스)
    print("[4/5] check_minimum_wage 호출 (기본급 190만 + 격월상여 60만, 2026년)...")
    try:
        result, _ = call(url, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "check_minimum_wage", "arguments": {
                "items": [
                    {"명칭": "기본급", "금액": 1900000, "지급주기": "매월", "성격": "기본급"},
                    {"명칭": "상여금", "금액": 600000, "지급주기": "격월", "성격": "정기상여"},
                ],
                "weekly_hours": 40, "year": 2026,
            }},
        }, session_id)
        parsed = tool_result(result)
        r = parsed.get("결과", {})
        if r.get("위반여부") is True:
            print(f"    성공 — 위반 검출 (비교시급 {r.get('비교시급')}원 < 최저 {r.get('최저시급')}원)")
        else:
            print(f"    경고 — 위반이 검출되어야 하는 케이스인데 판정이 다름: {r}")
            ok = False
    except Exception as e:
        print(f"    실패: {e}")
        ok = False

    # 5) tools/call - moel_interpretation_search (law.go.kr 실호출 — OC 키·IP 검증)
    print("[5/5] moel_interpretation_search 호출 (키워드: 연차)...")
    try:
        result, _ = call(url, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "moel_interpretation_search",
                       "arguments": {"keyword": "연차", "display": 3}},
        }, session_id)
        parsed = tool_result(result)
        if "오류" in parsed:
            print(f"    경고 — law.go.kr 호출 오류 (OC 키/IP 등록 확인): {parsed['오류']}")
            ok = False
        else:
            n_moel = len(parsed.get("고용노동부_행정해석", []))
            n_moleg = len(parsed.get("법제처_법령해석례", []))
            print(f"    성공 — 고용노동부 행정해석 {n_moel}건 + 법제처 해석례 {n_moleg}건")
    except Exception as e:
        print(f"    실패: {e}")
        ok = False

    print()
    if ok:
        print("=== 결과: 서버 정상 작동 중입니다. ===")
        print("Claude 쪽에서 도구가 안 보인다면 서버 문제가 아니라 Claude 커넥터 인식/캐싱 쪽 문제입니다.")
    else:
        print("=== 결과: 일부 항목에서 문제가 발견되었습니다. 위 로그를 확인해 주세요. ===")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="labor-mcp 서버 독립 검증 스크립트")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8735/mcp",
        help="테스트할 MCP 서버 URL (기본값: 로컬 8735)",
    )
    args = parser.parse_args()

    success = run(args.url)
    sys.exit(0 if success else 1)
