# setup_refresh_task.ps1 — 사례 아카이브 6개월 주기 갱신 작업 등록 (관리자 PowerShell)
# 사용: .\setup_refresh_task.ps1            # 매년 1월 1일·7월 1일 02:00에 refresh_archive.bat 실행
#       .\setup_refresh_task.ps1 -RunNow    # 등록 직후 한 번 바로 실행
# - SYSTEM 계정, 실행 시간 제한 없음(전체 재적재는 10시간 넘게 걸릴 수 있음)
# - 이미 있으면 덮어쓴다 (/F). 결과는 setup_task.log에 남긴다.
param([switch]$RunNow)
$ErrorActionPreference = "Continue"
$log = "D:\Server\Labor-mcp-server\setup_task.log"
Start-Transcript -Path $log -Append

$bat = "D:\Server\Labor-mcp-server\refresh_archive.bat"
$name = "labor-mcp-archive-refresh"
# 6개월 주기: schtasks MONTHLY는 /MO 최대 12이지만 시작 월 기준이라 1월·7월을 명시하는 편이 확실하다
schtasks /Create /TN $name /TR "`"$bat`"" /SC MONTHLY /M JAN,JUL /D 1 /ST 02:00 /RU SYSTEM /RL HIGHEST /F

$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Set-ScheduledTask -TaskName $name -Settings $s | Out-Null
Write-Output "등록 완료: $name (매년 1/1·7/1 02:00, 놓치면 다음 기동 시 실행)"
schtasks /Query /TN $name /FO LIST /V | Select-String "다음 실행 시간|Next Run Time|상태|Status"

if ($RunNow) {
    schtasks /Run /TN $name
    Write-Output "즉시 실행 요청 — 진행은 data\ingest.log 참고"
}
Stop-Transcript
