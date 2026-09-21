#!/bin/sh
set -eu

script="${PLUGIN_ROOT:?PLUGIN_ROOT is not set}/scripts/metrics.py"

if command -v python3 >/dev/null 2>&1; then
    exec "$(command -v python3)" -B "$script" hook
fi

for python in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$python" ]; then
        exec "$python" -B "$script" hook
    fi
done

echo "codex-toolkit: python3 is not available in the hook execution environment" >&2
exit 127
