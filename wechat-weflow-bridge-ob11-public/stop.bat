@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   停止 Akasha-WeChat 服务
echo ============================================

call :killport 8766
call :killport 11229
call :killport 8080

if exist bridge.pid (
    for /f %%p in (bridge.pid) do taskkill /PID %%p /T /F >nul 2>&1
)
if exist backend.pid (
    for /f %%p in (backend.pid) do taskkill /PID %%p /T /F >nul 2>&1
)

echo.
echo 已尝试停止全部服务（含后端自动拉起的 codebuddy --serve）。
echo ============================================
pause
endlocal
goto :eof

:killport
set "P=%1"
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%P%" ^| findstr "LISTENING"') do (
    echo [停止] 端口 %P% 对应进程 PID %%a
    taskkill /PID %%a /T /F >nul 2>&1
)
goto :eof
