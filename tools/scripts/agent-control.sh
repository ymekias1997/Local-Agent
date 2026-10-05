#!/usr/bin/env bash
# Keep terminal controls and the dashboard on the same Python implementation.
# Quoted forwarding preserves spaces and prevents task text becoming shell code.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${script_dir}/..${PYTHONPATH:+:${PYTHONPATH}}"
exec "${LOCAL_LLM_PYTHON:-python3}" -m local_llm_tools.service "$@"
