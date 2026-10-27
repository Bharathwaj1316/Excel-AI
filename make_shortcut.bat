@echo off
REM Puts an "Excel AI Filter" icon on the desktop that starts the app.
REM Run again after moving this folder, so the icon points to the new place.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Excel AI Filter.lnk');" ^
  "$s.TargetPath = '%~dp0run.bat';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.IconLocation = '%~dp0app.ico';" ^
  "$s.Description = 'Filter Excel files offline';" ^
  "$s.Save()"
if errorlevel 1 (
    echo Couldn't create the desktop icon.
) else (
    echo Desktop icon "Excel AI Filter" created.
)
