@echo off
chcp 65001 >nul
rem 本地测试环境一键启动：双击即可，关掉窗口就停。
rem 控制台 http://127.0.0.1:6670  ·  视频源 6680
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=python
)

echo 使用解释器：%PY%
%PY% tools\devserver.py --port 6670 --media-port 6680
pause
