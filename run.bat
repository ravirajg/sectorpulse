@echo off
cd /d "%~dp0"
set SSLKEYLOGFILE=
python -m pip install -r requirements.txt -q
python main.py
if errorlevel 1 pause
