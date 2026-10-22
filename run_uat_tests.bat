@echo off
setlocal
cd /d "%~dp0"
rem UAT order tests from a terminal. The same tests run from the screen:
rem   Order tests (top bar) - http://127.0.0.1:8000/order-tests
rem Usage: run_uat_tests.bat esz6        (or: run_uat_tests.bat esz6 --only M1,M3)
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m fixtrader.uat --contract %*
pause
