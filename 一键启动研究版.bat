@echo off
chcp 65001 >nul
title AI量化交易控制台 (含股票研究)
cd /d D:\destok\money
echo ============================================
echo   AI量化交易控制台 一键启动 (含股票研究)
echo   前端+交易+自托管+研究+复盘 全部内置
echo ============================================
echo.
echo 正在启动服务器... 稍候将自动打开浏览器
echo 地址: http://127.0.0.1:8080
echo.
REM 检查 8080 端口是否已被占用
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [提示] 8080 端口已被占用, 可能已有一个实例在运行。
    echo        如果页面打不开, 请先关闭旧的窗口再启动。
    echo.
)
REM 启动服务器并自动打开浏览器
start "" "http://127.0.0.1:8080"
D:\py\python.exe frontend/api_server.py
pause
