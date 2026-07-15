#!/usr/bin/env bash
set -euo pipefail

: "${COPILOT_CONFIG:?COPILOT_CONFIG must point to the external server config}"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
RELEASES_DIR="${WORKBUDDY_RELEASES_DIR:-/srv/workbuddy-copilot/releases}"

case "$($PYTHON_BIN --version 2>&1)" in
  "Python 3.13."*) ;;
  *) echo "Python 3.13 is required" >&2; exit 1 ;;
esac

"$PYTHON_BIN" - "$COPILOT_CONFIG" "$RELEASES_DIR" <<'PY'
import json
import os
from pathlib import Path
import sys

config_path = Path(sys.argv[1]).expanduser()
releases = Path(sys.argv[2]).expanduser().resolve()
if not config_path.is_absolute() or not config_path.is_file():
    raise SystemExit("COPILOT_CONFIG must be an existing absolute path")
with config_path.open(encoding="utf-8") as handle:
    db_path = Path(json.load(handle)["store"]["db_path"]).expanduser()
if not db_path.is_absolute():
    raise SystemExit("store.db_path must be absolute")
if config_path.resolve().is_relative_to(releases):
    raise SystemExit("COPILOT_CONFIG must live outside releases")
if db_path.resolve().is_relative_to(releases):
    raise SystemExit("store.db_path must live outside releases")
if not db_path.parent.is_dir() or not os.access(db_path.parent, os.W_OK):
    raise SystemExit("store.db_path parent must exist and be writable")
PY
