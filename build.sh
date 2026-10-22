#!/usr/bin/env bash
# HYDRA_UMC_SCRIPT_STANDARD_HEADER_BEGIN
# *****************************************************************************
# Project   : HYDRA-UMC-DATALAKE
# Script    : build.sh
# Purpose   : Incremental project build, verification and packaging workflow.
# Author    : JuanenRac (Electro Hobby 3D)
# Email     : electrohobby3d@gmail.com
# Copyright : (C) 2026 JuanenRac
# License   : GPL-3.0-or-later - see LICENSE
# *****************************************************************************
# HYDRA_UMC_SCRIPT_STANDARD_HEADER_END
# HYDRA_UMC_SCRIPT_STANDARD_BANNER_BEGIN
printf '\n*******************************************************************************\n'
printf '%s\n' "* HYDRA-UMC-DATALAKE - build.sh"
printf '%s\n' "* Mode      : INCREMENTAL BUILD"
printf '%s\n' "* Author    : JuanenRac (Electro Hobby 3D)"
printf '%s\n' "* Email     : electrohobby3d@gmail.com"
printf '%s\n' "* Copyright : (C) 2026 JuanenRac"
printf '%s\n' "* License   : GPL-3.0-or-later - see LICENSE"
printf '%s\n' "* ------------------------------------------------------------------------- *"
printf '%s\n' "* 1. Read the project version (building never changes it; use the explicit bump command)."
printf '%s\n' "* 2. Run this project's declared build, verification and packaging commands."
printf '%s\n' "* 3. Report the result and keep an interactive terminal open."
printf '%s\n' "*******************************************************************************"
printf '\n'
# HYDRA_UMC_SCRIPT_STANDARD_BANNER_END
set -euo pipefail
cd "$(dirname "$0")"

# Keep the window open if this was double-clicked (e.g. from a file
# manager) instead of run from an already-open terminal - fires on
# success AND on a `set -e` early exit alike, but only prompts when
# stdin is actually a terminal (never in CI/piped/non-interactive runs).
trap '[ -t 0 ] && read -r -p "Press Enter to close..." _' EXIT
# HYDRA_UMC_SCRIPT_STANDARD_VERSION_STEP
printf '%s\n' "[1/5] Reading the project version (building never changes it)..."
HYDRA_UMC_VERSION_BUILT="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["version"])' "$(dirname "$0")/hydra-umc.project.json")"
printf '%s\n' "      Building v${HYDRA_UMC_VERSION_BUILT:-unknown} (building never changes the version)"
echo
echo "      Done."
echo

echo "[2/5] Creating/activating virtual environment..."
# The venv is made with python3 (or python); the scripts use the python of the venv directly instead of an activate script: the python that is first on the PATH
# may be a stripped one (for example the one of ESP-IDF) whose venv has none.
if [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python.exe ]; then
    (python3 -m venv .venv 2>/dev/null || python -m venv .venv)
fi
if [ -x .venv/bin/python ]; then
    VPY=".venv/bin/python"
elif [ -x .venv/Scripts/python.exe ]; then
    VPY=".venv/Scripts/python.exe"
else
    echo "ERROR: the venv has no python; delete the .venv folder and run this again." >&2
    exit 1
fi
echo "      Done."
echo

echo "[3/5] Installing project (editable, with dev extras) into the venv..."
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install -e ".[dev]"
echo "      Done."
echo

echo "[4/5] Verifying the package compiles/imports without errors..."
"$VPY" -m py_compile src/hydra_umc_datalake/__init__.py src/hydra_umc_datalake/main.py
"$VPY" -c "import hydra_umc_datalake; print('import OK - version', hydra_umc_datalake.__version__)"
echo "      Done."
echo

echo "[5/5] Running the real test suite (pytest)..."
"$VPY" -m pytest tests/ -q
echo "      Done."
echo

echo " ==============================================================="
echo "  Build complete. Run ./run.sh to start the HTTP API."
echo "  Run docker-compose.yml to bring up the full Datalake stack"
echo "  (Datalake + Telemetry-Collector + Anomaly-Detector +"
echo "  Production-Reports)."
echo " ==============================================================="
echo
printf '%s\n' "Built v${HYDRA_UMC_VERSION_BUILT:-unknown}. Building never changes the version; to raise it run: python tools/hydra_project_tool.py bump"
