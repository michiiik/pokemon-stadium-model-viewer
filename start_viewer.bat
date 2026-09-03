@echo off
setlocal

set "REPO_ROOT=%~dp0"
set "CONFIG=%REPO_ROOT%viewer.local.json"
set "PORT=8767"

if not exist "%CONFIG%" (
  echo No viewer.local.json found.
  echo Run: py -3 tools\setup_viewer.py --stadium1-rom "path\to\pokemon-stadium-1.z64" --stadium2-rom "path\to\stadium2.z64"
  exit /b 2
)

echo Starting Pokemon Stadium Model Viewer on http://127.0.0.1:%PORT%/
start "Pokemon Stadium Model Viewer" /D "%REPO_ROOT%" py -3 tools\stadium1_viewer.py --config "%CONFIG%" --dual --port %PORT% --open

endlocal
