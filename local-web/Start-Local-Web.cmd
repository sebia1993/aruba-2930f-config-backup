@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" -B -X utf8 "%~dp0server.py"
  exit /b 0
)
echo Extract the entire Windows x64 release ZIP before starting.
pause
exit /b 1
