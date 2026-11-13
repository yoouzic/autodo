@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0login-doubao.ps1"
set "LOGIN_RESULT=%ERRORLEVEL%"
pause
exit /b %LOGIN_RESULT%
