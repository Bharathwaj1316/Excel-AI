@echo off
REM One-time setup (needs internet). After this the app runs fully offline.
cd /d "%~dp0"

where ollama >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    echo Ollama is not installed. Install it from https://ollama.com/download then run setup.bat again.
    pause
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
    echo Creating Python environment...
    py -3.12 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo Downloading the AI model (about 2 GB)...
"%LOCALAPPDATA%\Programs\Ollama\ollama.exe" pull qwen2.5:3b || ollama pull qwen2.5:3b

call make_shortcut.bat

echo.
echo Setup finished. Double-click "Excel AI Filter" on the desktop to start the app.
pause
