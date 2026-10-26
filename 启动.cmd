@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem 优先 py 启动器（Windows 官方），其次 PATH 上的 python。
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -X utf8 launcher.py %*
    goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
    python -X utf8 launcher.py %*
    goto :done
)
echo 找不到 Python。请安装 Python 3.10+ 并确保它在 PATH 上。
pause
exit /b 1

:done
if errorlevel 1 pause
