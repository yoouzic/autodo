@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -File "%~dp0scripts\launch.ps1" %*
set "launchExit=%ERRORLEVEL%"
if "%launchExit%"=="0" exit /b 0
echo.
echo AuToDo could not open. Review the error above.
echo This window will stay open until you press a key.
pause >nul
exit /b %launchExit%
