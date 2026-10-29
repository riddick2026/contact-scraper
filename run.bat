@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Python not found. Install it from https://www.python.org/downloads/ and tick "Add to PATH". & pause & exit /b 1)
)
echo Installing requirements...
.venv\Scripts\python -m pip install -q -r requirements.txt
echo Starting Contact Scraper at http://localhost:8501 ...
.venv\Scripts\streamlit run app.py
pause
