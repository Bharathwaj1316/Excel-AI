@echo off
REM Starts Excel AI Filter in your browser at http://localhost:8501
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First time? Run setup.bat first.
    pause
    exit /b 1
)
REM localhost: only this computer can open the app, not others on the network.
.venv\Scripts\python.exe -m streamlit run app.py --server.address localhost
