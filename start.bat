@echo off
setlocal EnableExtensions
rem --- IMPORTANT: keep this file pure ASCII. Any Chinese text here is UTF-8,
rem --- but cmd parses batch files in GBK, which shreds the script and makes
rem --- the console window flash and close (see pitfall record, problem 2).
rem --- All real startup logic lives in launcher.py (UTF-8, step-by-step,
rem --- auto venv + deps, port pre-check, health check, auto-open browser).
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)
python "%~dp0launcher.py" %*
endlocal
exit /b %errorlevel%
