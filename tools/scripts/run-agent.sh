#!/usr/bin/env bash
# Run from any directory; preserve argument boundaries without evaluating text.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${script_dir}/..${PYTHONPATH:+:${PYTHONPATH}}"
exec "${LOCAL_LLM_PYTHON:-python3}" -m local_llm_tools "$@"
