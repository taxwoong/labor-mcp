@echo off
rem refresh_archive.bat - incremental refresh of the labor case archive (SQLite).
rem Task Scheduler runs this every 6 months (see setup_refresh_task.ps1); double-click works too.
rem Keep this file ASCII + CRLF (cmd reads CP949 - see run_server.bat note).
rem Log: data\ingest.log   Env: local_env.bat (LAW_API_OC, DATA_GO_KR_KEY)
cd /d %~dp0
if exist "%~dp0local_env.bat" call "%~dp0local_env.bat"
if not exist "%~dp0data" mkdir "%~dp0data"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ===== refresh start %date% %time% ===== >> "%~dp0data\ingest.log"
"%~dp0.venv\Scripts\python.exe" "%~dp0ingest_archive.py" all --interval 0.3 >> "%~dp0data\ingest.log" 2>&1
echo ===== refresh end %date% %time% exit %errorlevel% ===== >> "%~dp0data\ingest.log"
