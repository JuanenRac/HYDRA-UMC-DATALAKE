@echo off
REM =============================================================================
REM HYDRA-UMC-DATALAKE - build.bat
REM Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
REM GPL-3.0 - see LICENSE
REM =============================================================================
REM Builds HYDRA-UMC-DATALAKE: creates/activates a venv, installs the
REM project (editable, with dev extras), verifies it imports cleanly, and
REM runs the real test suite. Run this before run.bat.
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ===============================================================
echo   H Y D R A - U M C - D A T A L A K E  -  build
echo  ===============================================================
echo   Scalable time-series storage (parent: TELEMETRY-COLLECTOR /
echo   ANOMALY-DETECTOR / PRODUCTION-REPORTS)
echo   Author:  JuanenRac (Electro Hobby 3D)
echo   License: GPL-3.0 (see LICENSE.md)
echo  ===============================================================
echo.

echo [1/5] Bumping version number (odometer bump, see bump_version.py)...
python bump_version.py
if errorlevel 1 ( echo NATIVE VERSION BUMP FAILED. & pause & exit /b 1 )
python "%~dp0bump_manifest_version.py" --sync
if errorlevel 1 ( echo VERSION SYNCHRONIZATION FAILED. & pause & exit /b 1 )
if errorlevel 1 goto :error
echo       Done.
echo.

echo [2/5] Creating/activating virtual environment...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 goto :error
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto :error
echo       Done.
echo.

echo [3/5] Installing project (editable, with dev extras) into the venv...
python -m pip install --upgrade pip >nul
if errorlevel 1 goto :error
python -m pip install -e ".[dev]"
if errorlevel 1 goto :error
echo       Done.
echo.

echo [4/5] Verifying the package compiles/imports without errors...
python -m py_compile src\hydra_umc_datalake\__init__.py src\hydra_umc_datalake\main.py
if errorlevel 1 goto :error
python -c "import hydra_umc_datalake; print('import OK - version', hydra_umc_datalake.__version__)"
if errorlevel 1 goto :error
echo       Done.
echo.

echo [5/5] Running the real test suite (pytest)...
python -m pytest tests/ -q
if errorlevel 1 goto :error
echo       Done.
echo.

echo  ===============================================================
echo   Build complete. Run run.bat to start the HTTP API.
echo   Run docker-compose.yml to bring up the full Datalake stack
echo   (Datalake + Telemetry-Collector + Anomaly-Detector +
echo   Production-Reports).
echo  ===============================================================
echo.
pause
exit /b 0

:error
echo.
echo   BUILD FAILED - see the output above.
pause
exit /b 1
