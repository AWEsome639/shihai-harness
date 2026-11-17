#!/usr/bin/env bash
set -euo pipefail
exec python -B /tests/harbor_adapter.py --config /tests/adapter_config.json
