@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 launcher.py
exit /b %ERRORLEVEL%
:use_python
python launcher.py
exit /b %ERRORLEVEL%
