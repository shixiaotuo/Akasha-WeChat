@echo off
setlocal
cd /d "%~dp0"

set "BACKEND_TITLE=Akasha-Backend"
set "BRIDGE_TITLE=Akasha-Bridge"
set "PY=python"

REM ---------- 清理上次残留的 pid 文件（仅当对应服务未运行时） ----------
netstat -ano 2>nul | findstr ":8766" | findstr "LISTENING" >nul || if exist bridge.pid ( del /q bridge.pid )
netstat -ano 2>nul | findstr ":11229" | findstr "LISTENING" >nul || if exist backend.pid ( del /q backend.pid )

echo ============================================
echo   Akasha-WeChat 一键启动 (后端 + 桥接)
echo ============================================

REM ---------- 1. 启动 WorkBuddy 后端 (端口 11229) ----------
set "BACKEND_UP=0"
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":11229" ^| findstr "LISTENING"') do set "BACKEND_UP=1"

if "%BACKEND_UP%"=="1" (
    echo [跳过] 后端已在运行 (端口 11229 已被占用)
) else (
    echo [启动] WorkBuddy 后端 (workbuddy_backend.py) ...
    start "%BACKEND_TITLE%" /min "%PY%" workbuddy_backend.py
    timeout /t 1 >nul
    for /f "tokens=2" %%p in ('tasklist /fi "windowtitle eq %BACKEND_TITLE%" /fo list 2^>nul ^| find "PID:"') do (
        echo %%p>backend.pid
        echo        后端 PID=%%p
    )
)

REM ---------- 2. 启动桥接 (端口 8766) ----------
set "BRIDGE_UP=0"
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8766" ^| findstr "LISTENING"') do set "BRIDGE_UP=1"

if "%BRIDGE_UP%"=="1" (
    echo [跳过] 桥接已在运行 (端口 8766 已被占用)
) else (
    echo [启动] 桥接 (main.py) ...
    start "%BRIDGE_TITLE%" "%PY%" main.py
)

echo.
echo 启动完成。Web 控制面板: http://127.0.0.1:8766
echo 关闭请运行 stop.bat
echo ============================================
pause
endlocal
