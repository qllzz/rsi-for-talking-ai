#!/bin/zsh
set -eu
cd "$(dirname "$0")"

export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PWD/.cache/python}"

if [[ ! -x .venv/bin/python ]]; then
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.13 .venv
    elif command -v python3.13 >/dev/null 2>&1; then
        python3.13 -m venv .venv
    else
        echo '请先安装 Python 3.13 或 uv，再重新启动。'
        exit 1
    fi
fi

exec .venv/bin/python -X utf8 scripts/launch.py "$@"
