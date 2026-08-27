@echo off
REM =============================================================================
REM HYDRA-UMC-DATALAKE - run.bat
REM Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
REM GPL-3.0 - see LICENSE
REM =============================================================================
REM Runs HYDRA-UMC-DATALAKE's entry point. Run build.bat first.
cd /d "%~dp0"

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

python -m hydra_umc_datalake.main %*
pause
