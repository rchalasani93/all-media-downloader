@echo off
cd /d "%~dp0"
if not exist .venv (
  echo First run: setting up. This takes a minute.
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" http://127.0.0.1:8765
.venv\Scripts\python app.py
pause
