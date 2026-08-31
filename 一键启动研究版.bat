@echo off
chcp 65001 >nul
title AI量化交易控制台 (含股票研究)
cd /d D:\destok\money
echo ============================================
echo   AI量化交易控制台 一键启动 (含股票研究)
echo   前端+交易+自托管+研究+复盘 全部内置
echo ============================================
echo.

REM === 0. 检查 8080 是否已有实例在跑 (避免新旧叠加, 新实例绑定会失败) ===
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [提示] 8080 端口已有服务器在运行。
    echo        直接打开浏览器即可, 无需重复启动。
    echo 地址: http://127.0.0.1:8080
    echo.
    start "" "http://127.0.0.1:8080"
    pause
    exit /b 0
)

REM === 1. 后台启动服务器 (日志写到 logs/server_auto.log) ===
echo 正在启动服务器, 请稍候...
start "AI量化后端" /min cmd /c "D:\py\python.exe frontend\api_server.py >> logs\server_auto.log 2>&1"

REM === 2. 轮询等待端口就绪 (最多 60 秒) ===
set /a tried=0
:wait_loop
set /a tried+=1
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if %errorlevel%==0 goto ready
if %tried% geq 60 goto timeout
goto wait_loop

:timeout
echo.
echo [错误] 60 秒内服务器未能启动, 请查看 logs\server_auto.log 排查。
pause
exit /b 1

:ready
echo.
echo [成功] 服务器已就绪 (耗时约 %tried% 秒), 正在打开浏览器...
echo 地址: http://127.0.0.1:8080
echo 若页面显示"连接中", 请按 F5 刷新一次即可。
echo.
start "" "http://127.0.0.1:8080"
pause
