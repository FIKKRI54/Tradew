#!/usr/bin/env bash
set -euo pipefail

export WINEPREFIX=${WINEPREFIX:-$HOME/.mt5}
export DISPLAY=${DISPLAY:-:2}
INSTALL_DIR=${TRADEW_INSTALLER_DIR:-$HOME/tradew-installers}
INSTALLER=$INSTALL_DIR/python-3.12.10-amd64.exe
PYTHON=$WINEPREFIX/drive_c/Python312/python.exe

mkdir -p "$INSTALL_DIR"
if [ ! -s "$INSTALLER" ]; then
  curl --fail --location --output "$INSTALLER" \
    https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
fi

if [ ! -f "$PYTHON" ]; then
  wine "$INSTALLER" /quiet InstallAllUsers=0 'TargetDir=C:\Python312' \
    Include_pip=1 Include_test=0 Include_launcher=0 PrependPath=0
fi

REQUIREMENTS=$(winepath -w "$HOME/tradew-app/current/deploy/requirements-windows.txt")
wine "$PYTHON" -m pip install --disable-pip-version-check -r "$REQUIREMENTS"
wine "$PYTHON" -c 'import MetaTrader5, flask, waitress; print("Wine Python bridge dependencies ready")'
