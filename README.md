# labor-mcp

노무(노동법) 업무 특화 MCP 서버. Claude에서 근로계약서·취업규칙 검토/작성,
노동법 리서치, 임금대장 분석, 급여테이블 설계를 지원합니다.

**기능 5축**: ①근로계약서 검토·작성 ②취업규칙 검토·작성 ③노동법 문제 해결
(법령·판례·행정해석·판정례 근거) ④임금대장 분석(최저임금·통상/평균임금·주52시간·
가산수당·기재사항) ⑤급여테이블 역산 설계

설계·리서치 배경은 `개발계획.md`, 원자료는 `research/` 참고.
자매 프로젝트: [nts-mcp-server](../nts-mcp-server) (국세·지방세) — law.go.kr
클라이언트(`law_go_kr.py`)와 인증(OC 키·IP 화이트리스트)을 공유합니다.

## 도구 15종

### 검색 4종 (실데이터 소스, 전부 실측 검증)

| 도구 | 소스 | 내용 |
|---|---|---|
| `labor_law_article` | law.go.kr | 노동법령 조문(현행/시점별)·부칙·연혁. 통칭 지원(근기법·기간제법 등), 법령ID 자동 필터 |
| `moel_interpretation_search` | law.go.kr `moelCgmExpc` + `expc` | **고용노동부 행정해석(질의회시)** — "근로기준정책과-3084" 등 실무 문서번호 그대로, 질의요지·회답 전문. 법제처 법령해석례 동시 검색 |
| `labor_case_search` | law.go.kr `prec` | 법원 노동판례(대법원·하급심) 검색·본문(판시사항·판결요지·전문) |
| `nlrc_decision_search` | nlrc.go.kr 스크래핑 | 노동위원회 판정례 4.4만+ 건 — 판정요지 인라인, 카테고리·판정일·관할위원회 필터 |

### 계산 8종 (결정론적, 판례·행정해석 변경 반영)

`check_minimum_wage`(산입범위 분해·수습 3요건), `calc_ordinary_avg_wage`(2024-12-19
전합 신법리·평균임금 3/12·통상임금 하한), `calc_annual_leave`(1년 기간제 11일·
다음날 재직 요건·회계연도 비교정산), `calc_weekly_holiday_pay`, `calc_overtime_pay`
(5인 미만 분기·중복 가산), `calc_severance_pay`(DC형 구분), `calc_dismissal_notice_pay`,
`calc_wage_cut_limit`(감급 한도 §95)

모든 계산 결과는 `{결과, 계산과정, 근거, 주의사항}` 4필드 규약.

### 분석·설계 2종 + 리소스 열람 1종

- `analyze_payroll` — 임금대장 일괄 분석 (직원×월 6종 검사, 위반|적법|불확실 3단계
  판정, 상습 임금체불 제재 경고). 입력 스키마: `labor_resource("schema/임금대장-입력")`
- `design_pay_table` — 조건 입력 → 기본급+고정OT 대수적 분해 → 최저임금 역검증 →
  근기법 17조② 형식 계약서 임금조항 문안 생성
- `labor_resource` — 지식 리소스 11종 열람 (claude.ai에서 MCP resources가 UI에
  안 보이는 경우 대비 이중 노출)

### 리소스 11종 (`labor://` URI)

체크리스트 2(근로계약서·취업규칙), 판정표 4(임금항목 산입매트릭스·5인미만
적용제외·시행중 개정법 기준선·최저임금 연도별), 입력 스키마 1(임금대장),
템플릿 4(2026 표준취업규칙 일반 98개조·단시간 96개조·직장내괴롭힘 표준안·
2025 표준근로계약서 서식 6종)

### 프롬프트 2종

`review_employment_contract`, `review_work_rules` — 체크리스트 로드→조문 대조→
근거 조회→수치 검증→위반 항목별 {조항 인용/근거/무효 시 대체 기준/수정 문안}
출력 순서를 강제하는 검토 절차 템플릿.

## 실행

```bash
pip install -r requirements.txt          # mcp[cli]<2.0 고정 (2.0에서 FastMCP 제거됨)
run_server.bat                           # PORT=8735, local_env.bat에서 LAW_API_OC 로드
python test_mcp_client.py                # 5단계 스모크 테스트 (초기화→도구목록→리소스→계산→실호출)
python -m pytest tests -q               # 단위 테스트 (오프라인만: -m "not live")
```

- `local_env.bat`(미커밋): `set LAW_API_OC=본인_기관코드` 한 줄. nts-mcp-server와
  동일한 키를 쓴다 (같은 서버컴퓨터 → IP 화이트리스트 추가 등록 불필요).
- **run_server.bat은 ASCII + CRLF 유지**: cmd가 CP949로 읽기 때문에 UTF-8 한국어
  주석이 줄바꿈을 삼켜 `call local_env.bat`이 통째로 무시되는 장애를 실제로 겪었다
  (2026-08-28). 경로는 전부 `%~dp0` 절대경로 — 경로 없는 `call local_env.bat`은
  `NoDefaultCurrentDirectoryInExePath` 설정 환경에서 실패한다.

## 배포 (서버컴퓨터 + Tailscale Funnel)

국세 서버(8734→Funnel 443 루트)와 같은 컴퓨터에서 상시 구동한다. 노무 서버는
같은 443 포트 아래 **비밀 경로**로 노출한다 — 이 서버는 인증이 없으므로
무작위 경로가 사실상 접근 토큰 역할을 한다. Funnel 호스트명은 인증서 발급
과정에서 CT 로그에 공개되므로 호스트명 비밀 유지는 애초에 불가능하고, 경로만이
비밀로 유지 가능하다(경로는 TLS로 암호화되어 전송됨).

```powershell
# 1) 작업 스케줄러 등록 + Funnel 노출 (관리자 PowerShell — 비밀 경로는 임의 생성)
.\setup_task_and_funnel.ps1 -FunnelPath "/labor-<랜덤토큰>"
```

MCP 서버 URL: `https://<호스트>.ts.net/labor-<랜덤토큰>/mcp`
— **실제 호스트명·경로는 이 저장소에 기록하지 않는다** (공개 repo). 운영 값은
서버컴퓨터의 `운영정보.local.md`(.gitignore 처리) 참고. Tailscale이 경로 접두사를
벗겨 전달하므로 FastMCP `/mcp`와 그대로 맞물린다 (2026-08-28 실측 검증).
경로가 유출되면 `tailscale funnel --set-path=<옛경로> off` 후 새 경로로 재발급
(커넥터 URL도 재등록).

claude.ai → 설정 → 커넥터 → 사용자 지정 커넥터 추가 → 위 URL 등록 →
도구 권한 "항상 허용" → **완전히 새 대화창**에서 도구 인식 확인.
(국세 커넥터와 별개 커넥터로 등록 — 한 대화에서 둘을 함께 쓰는 것을 권장:
행정규칙·조약 등 일반 법령 도구와 세법 비과세 확인은 국세 커넥터가 담당)

### 커넥터 등록 시 "로그인 서비스에 등록할 수 없습니다"(OAuth) 오류가 뜨면

이 서버는 인증을 요구하지 않는다 — 2026-08-28 진단에서 OAuth 관련 전 경로
(`/.well-known/oauth-*`, `/register`)가 nts 서버와 **완전히 동일하게** 404(인증
불요)를 반환함을 확인했다. 이 오류는 claude.ai 쪽 등록 플로우 문제다. 순서대로:
1. 등록 대화상자를 완전히 닫고 재시도 (OAuth Client ID 칸은 비워 둘 것)
2. 판별 테스트: 잘 동작 중인 국세 커넥터의 URL로 임시 커넥터를 새로 추가해 본다.
   그것도 같은 오류면 claude.ai 일시 장애 — 시간을 두고 재시도하거나
   support.claude.com에 참조 코드(ofid_...)를 전달
3. 서버 쪽 확인이 필요하면 `python test_mcp_client.py --url <공개URL>` (5단계 전부
   성공하면 서버 문제 아님)

## 파일 구성

```
labor-mcp/
├── server.py             # MCP 서버 본체 (FastMCP) — 도구 15 + 리소스 11 + 프롬프트 2
├── law_go_kr.py          # law.go.kr Open API 클라이언트 (nts-mcp-server에서 이식)
├── moel_expc.py          # 고용노동부 행정해석(moelCgmExpc) 클라이언트
├── nlrc.py               # 노동위원회 판정례 스크래퍼 (non-www 필수 — www는 POST 본문 유실)
├── calculators.py        # 노무 계산 엔진 11함수 (순수 함수)
├── payroll.py            # 임금대장 분석기 + 급여테이블 설계기
├── labor_constants.py    # 연도별 파라미터(최저임금·산입비율)·법령ID — 매년 갱신 지점
├── resources/            # 지식 리소스 11종 (체크리스트·판정표·표준 서식)
├── tests/                # pytest 105건 (계산기 51 + 임금대장 34 + nlrc 20)
├── research/             # 리서치 원자료 (표준취업규칙 hwp, nlrc 응답 샘플 등)
├── test_mcp_client.py    # 커넥터 우회 독립 점검 (5단계)
├── run_server.bat        # 상시 구동용 (ASCII+CRLF 유지 — 위 주의사항)
└── local_env.bat         # (미커밋) LAW_API_OC
```

## 연간 갱신 체크리스트 (개발계획.md Phase 6)

| 시기 | 작업 |
|---|---|
| 매년 8월 초 | 최저임금 고시 확인 → `labor_constants.MINIMUM_WAGE`에 이듬해분 추가 + `resources/최저임금_연도별.md` 갱신 (미등록 연도는 계산 도구가 명시적 오류를 내도록 설계됨) |
| 매년 초 | 고용노동부 표준취업규칙·표준근로계약서 개정본 게시 확인 (moel.go.kr 정책자료실) → hwp 변환 재실행 (`research/hwp_toc.py` 참고) |
| 수시 | `resources/시행중_개정법_기준선.md`의 "추진 중" 항목(정년연장·주4.5일제·5인 미만 확대·포괄임금 금지) 입법 통과 여부 |
| 장애 시 | nlrc.go.kr·moel.go.kr 화면 개편 → `tests/test_nlrc.py` 픽스처 교체 후 파서 보수 |

## 유의사항

- 검토·계산 결과는 참고용 — 최종 판단은 공인노무사 확인 필요 (서버 instructions와
  프롬프트에 명시됨). 사무소 내부용을 넘어 제3자 유료 자문으로 외부화할 경우
  공인노무사법(제2조·제27조) 검토 선행.
- 고용노동부 행정해석(moelCgmExpc)은 2026-07 해석까지 수록 확인 — 갱신 주기는
  미확인이므로 최신 쟁점은 판례·노동위 판정례로 교차 확인 권장.
