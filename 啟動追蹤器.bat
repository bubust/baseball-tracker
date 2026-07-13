@echo off
chcp 65001 >nul
title Baseball Tracker ⚾
cd /d "%~dp0"
echo ⚾ 棒球追蹤器啟動中...
python main.py
pause
