@echo off
rem labor-mcp server ? Task Scheduler runs this at boot. Port 8735 (nts=8734).
rem LAW_API_OC must NOT be committed ? it lives in local_env.bat (.gitignore'd):
rem   @echo off
rem   set LAW_API_OC=YOUR_OC_CODE
rem NOTE: use %~dp0 absolute paths ? bare "call local_env.bat" fails on systems
rem with NoDefaultCurrentDirectoryInExePath (observed 2026-08-28).
rem NOTE: keep this file ASCII + CRLF ? cmd reads CP949, UTF-8 Korean comments
rem can swallow line breaks (nts README documented a similar CRLF incident).
cd /d %~dp0
set PORT=8735
if exist "%~dp0local_env.bat" call "%~dp0local_env.bat"
if "%LAW_API_OC%"=="" echo [WARN] LAW_API_OC not set - law.go.kr tools will fail auth. Create local_env.bat. >> "%~dp0server.log"
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py" >> "%~dp0server.log" 2>&1
