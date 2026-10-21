@echo off
REM One-click start (unified single port 6677: API + frontend)
REM NOTE: keep this file ASCII-only. cmd.exe parses .bat with the OEM codepage
REM       (GBK on zh-CN Windows); UTF-8 Chinese here breaks command parsing.
setlocal enabledelayedexpansion

set "PORT=6677"
if not "%RAG_PORT%"=="" set "PORT=%RAG_PORT%"

echo [1/3] Init database and admin account...
cd /d "%~dp0backend"
if not exist ".venv" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if not exist ".env" copy .env.example .env >nul
.venv\Scripts\python.exe scripts\seed.py

echo [2/3] Build frontend (Vue)...
cd /d "%~dp0web"
if not exist "node_modules" npm install
call npm run build
if errorlevel 1 (
  echo [WARN] Frontend build failed; UI may be unavailable without web\dist.
)

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
cd /d "%~dp0backend"
start "RAG-Server" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --log-level warning"

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
endlocal
