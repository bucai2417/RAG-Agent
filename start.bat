@echo off
REM One-click start (unified single port 6677: API + frontend)
REM NOTE: keep this file ASCII-only. cmd.exe parses .bat with the OEM codepage
REM       (GBK on zh-CN Windows); UTF-8 Chinese here breaks command parsing.
setlocal enabledelayedexpansion

set "PORT=6677"
if not "%RAG_PORT%"=="" set "PORT=%RAG_PORT%"

set "ROOT=%~dp0"
set "PY=%ROOT%backend\.venv\Scripts\python.exe"

REM ---------------------------------------------------------------
REM 0) Already running? Just open the browser and exit.
REM    Each launch used to rebuild the frontend (~35s) and re-seed the DB
REM    every single time, even when nothing had changed.
REM ---------------------------------------------------------------
if exist "%PY%" (
  "%PY%" "%ROOT%backend\scripts\wait_ready.py" --port %PORT% --probe >nul 2>&1
  if not errorlevel 1 (
    echo Server already running on port %PORT%.
    start "" "http://127.0.0.1:%PORT%"
    endlocal
    exit /b 0
  )
)

echo [1/3] Checking Python environment...
cd /d "%ROOT%backend"
if not exist ".venv" (
  echo       creating venv and installing dependencies ^(one time only^)...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)
if not exist ".env" copy .env.example .env >nul

REM Seed is idempotent but still costs ~1.3s of import + DB round trips.
REM Only run it when the database does not exist yet (first run / wiped data).
set "NEED_SEED=0"
if not exist "data\rag.db" set "NEED_SEED=1"
if "%RAG_FORCE_SEED%"=="1" set "NEED_SEED=1"
if "%NEED_SEED%"=="1" (
  echo       initializing database and admin account...
  "%PY%" scripts\seed.py
) else (
  echo       database present, skip seed
)

echo [2/3] Checking frontend build...
cd /d "%ROOT%web"
if not exist "node_modules" (
  echo       installing npm dependencies ^(one time only^)...
  call npm install
)
REM Rebuild only when sources are newer than dist/index.html. vue-tsc type checking
REM is intentionally skipped here (adds ~17s); CI/Docker still run the full
REM "npm run build" so type errors are caught before release.
REM NOTE: the freshness check is Python, not PowerShell -- Get-ChildItem -Recurse
REM       took ~20s on this project while the os.scandir equivalent takes ~0.3s.
"%PY%" "%ROOT%backend\scripts\needs_build.py" --web-dir "%ROOT%web"
if errorlevel 1 goto :build
echo       frontend up to date, skip build
goto :built
:build
echo       sources changed, building frontend ^(vite only^)...
REM --logLevel warn keeps the 100+ asset lines out of the console; errors and
REM warnings are still printed, and the exit code is unchanged.
call npx --no-install vite build --logLevel warn
if errorlevel 1 (
  echo [WARN] Frontend build failed; UI may be unavailable without web\dist.
)
:built

REM ---- Probe LAN IPv4 (skip loopback / link-local / virtual adapter subnets) ----
set "LANIP="
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
  if "!LANIP!"=="" (
    set "IP=%%A"
    set "IP=!IP: =!"
    echo !IP! | findstr /b /c:"127." >nul
    if errorlevel 1 (
      echo !IP! | findstr /b /c:"169.254." >nul
      if errorlevel 1 (
        echo !IP! | findstr /b /c:"192.168.137." >nul
        if errorlevel 1 (
          echo !IP! | findstr /b /c:"192.168.56." >nul
          if errorlevel 1 set "LANIP=!IP!"
        )
      )
    )
  )
)

echo [3/3] Starting server on port %PORT% ...
cd /d "%ROOT%backend"
start "RAG-Server" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --log-level warning"

REM Wait for the port to answer instead of guessing; then open the browser.
"%PY%" scripts\wait_ready.py --port %PORT% --timeout 90 >nul 2>&1
if errorlevel 1 (
  echo [WARN] Backend did not become ready within 90s; check the RAG-Server window.
) else (
  start "" "http://127.0.0.1:%PORT%"
)

echo.
echo ============================================================
echo   Service starting. Open one of these in your browser:
echo.
echo     Local   : http://127.0.0.1:%PORT%
if not "!LANIP!"=="" (
  echo     Network : http://!LANIP!:%PORT%
  echo               ^(share this one with colleagues^)
) else (
  echo     Network : not detected - run ipconfig to find your IPv4
)
echo.
echo     Login   : admin / admin123
echo.
echo   If others can't open the Network address:
echo     1^) Allow Python through Windows Firewall when prompted
echo     2^) Make sure you are on the same LAN / VPN
echo ============================================================
echo.
REM Set RAG_FORCE_SEED=1 before running to re-run the idempotent seeder.
endlocal
