# setup_task_and_funnel.ps1 — labor-mcp 배포 스크립트 (관리자 권한 필요)
# 사용: .\setup_task_and_funnel.ps1 -FunnelPath "/labor-<랜덤토큰>"
#   (실제 운영 경로는 공개 repo에 적지 않는다 — 서버컴퓨터의 운영정보.local.md 참고.
#    새 토큰 생성 예: -join ((1..8) | % { (Get-Random -Max 256).ToString("x2") }))
# 1) 작업 스케줄러 등록 (부팅 시 SYSTEM으로 run_server.bat — nts-tax-mcp와 동일 패턴)
# 2) 기본 72시간 실행 제한 해제 (상시 서버가 3일마다 죽는 것 방지)
# 3) 수동 프로세스 종료 후 작업으로 재기동
# 4) Tailscale Funnel: 공개 443의 비밀 경로 → localhost:8735 (FunnelPath 지정 시)
# 결과는 setup_task.log에 기록된다.
param([string]$FunnelPath = "")
$ErrorActionPreference = "Continue"
$log = "D:\Server\Labor-mcp-server\setup_task.log"
Start-Transcript -Path $log -Force

$bat = "D:\Server\Labor-mcp-server\run_server.bat"
schtasks /Create /TN "labor-mcp" /TR "`"$bat`"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F

$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Set-ScheduledTask -TaskName "labor-mcp" -Settings $s | Out-Null
Write-Output "실행 시간 제한 해제 완료"

Get-NetTCPConnection -LocalPort 8735 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Sleep -Seconds 2
schtasks /Run /TN "labor-mcp"
Start-Sleep -Seconds 10
$l = Get-NetTCPConnection -LocalPort 8735 -State Listen -ErrorAction SilentlyContinue
if ($l) { Write-Output ("TASK-OK: 8735 listening, PID " + $l[0].OwningProcess) }
else { Write-Output "TASK-FAIL: 8735 not listening — server.log 확인 필요" }

if ($FunnelPath) {
    $ts = "C:\Program Files\Tailscale\tailscale.exe"
    & $ts funnel --bg --set-path=$FunnelPath localhost:8735
    Write-Output "--- funnel status ---"
    & $ts funnel status
} else {
    Write-Output "FunnelPath 미지정 — Funnel 노출은 건너뜀 (운영정보.local.md의 경로로 별도 실행)"
}

Stop-Transcript
