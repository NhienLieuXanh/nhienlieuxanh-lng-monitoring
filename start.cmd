@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="install-startup" goto :install_startup

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Chua co .venv. Chay truoc: python -m venv .venv
  echo roi: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  pause
  exit /b 1
)

sc query postgresql-x64-17 >nul 2>&1
if not errorlevel 1 (
  sc query postgresql-x64-17 | findstr /I "STOPPED" >nul
  if not errorlevel 1 (
    echo Dang bat PostgreSQL...
    net start postgresql-x64-17 >nul 2>&1
  )
)

"%PY%" -m app.cli check-db
if errorlevel 1 (
  echo Database chua san sang. Bat service postgresql-x64-17 roi chay lai start.cmd
  pause
  exit /b 1
)

"%PY%" -m alembic upgrade head
if errorlevel 1 (
  echo Migration that bai.
  pause
  exit /b 1
)

echo Mo dashboard: http://127.0.0.1:8000/ui/
echo Dong cua so nay = tat server.
"%PY%" -m app.cli serve
exit /b %ERRORLEVEL%

:install_startup
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = (Resolve-Path '%~dp0').Path; $startup = [Environment]::GetFolderPath('Startup'); $lnk = Join-Path $startup 'LNG Monitoring.lnk'; $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath = Join-Path $dir 'start.cmd'; $s.WorkingDirectory = $dir; $s.WindowStyle = 7; $s.Save(); Write-Output $lnk"
echo Da them vao Startup Windows — lan sau dang nhap se tu mo app.
pause
exit /b 0
