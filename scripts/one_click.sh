#!/usr/bin/env bash
# Linux / Termux / Git Bash entry point. Windows also has one_click.ps1.
set -eu
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD=python
else
  printf '未找到 Python。Docker 菜单需要 Python 3.9+；直跑需要 3.11+。Termux：pkg install python nodejs-lts git clang make rust pkg-config\n' >&2
  exit 1
fi
"${PYTHON_CMD}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "管理菜单需要 Python 3.9 或更新版本")'
export PYTHONUTF8=1
"${PYTHON_CMD}" "${SCRIPT_DIR}/install_shortcut.py"
exec "${PYTHON_CMD}" "${SCRIPT_DIR}/manage.py" "$@"
