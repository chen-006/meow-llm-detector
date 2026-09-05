#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 launch.py
fi
echo 'Please install Python 3.11+ / 请先安装 Python 3.11 或更新版本。' >&2
exit 1
