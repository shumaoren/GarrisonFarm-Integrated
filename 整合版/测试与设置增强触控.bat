@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0程序\launcher.ps1" -Mode touch
set "GF_RESULT=%ERRORLEVEL%"
if not "%GF_RESULT%"=="0" pause
exit /b %GF_RESULT%
