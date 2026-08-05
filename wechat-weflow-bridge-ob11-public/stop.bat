@echo off
setlocal
cd /d "%~dp0"

set "BACKEND_TITLE=Akasha-Backend"
set "BRIDGE_TITLE=Akasha-Bridge"

echo ============================================
echo   停止 Akasha-WeChat 服务
echo ============================================

REM 停止桥接（按 PID 文件）
if exist bridge.pid (
    for /f %%p in (bridge.pid) do (
        echo [停止] 桥接 PID %%p
        taskkill /PID %%p /T /F >nul 2>&1
    )
    del /q bridge.pid >nul 2>&1
) else (
    echo [提示] 未找到 bridge.pid（桥接可能未运行）
)

REM 停止后端（按 PID 文件，/T 会连带结束自动拉起的 codebuddy --serve 子进程）
if exist backend.pid (
    for /f %%p in (backend.pid) do (
        echo [停止] 后端 PID %%p
        taskkill /PID %%p /T /F >nul 2>&1
    )
    del /q backend.pid >nul 2>&1
) else (
    echo [提示] 未找到 backend.pid（后端可能未运行）
)

REM 兜底：按窗口标题清理（pid 文件缺失 / 过期时仍有效）
echo [兜底] 按窗口标题清理残留进程...
taskkill /fi "windowtitle eq %BRIDGE_TITLE%" /T /F >nul 2>&1
taskkill /fi "windowtitle eq %BACKEND_TITLE%" /T /F >nul 2>&1

echo.
echo 已尝试停止全部服务（含自动拉起的 serve）。
echo 若 8080 端口的 codebuddy --serve 仍有残留，可手动任务管理器结束 node 进程。
echo ============================================
pause
endlocal
