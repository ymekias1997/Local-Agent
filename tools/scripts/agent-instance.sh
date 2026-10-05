#!/usr/bin/env bash
# Launch and message persistent copies of the Python agent. All arguments are
# forwarded literally; task text is never evaluated as shell code.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${script_dir}/..${PYTHONPATH:+:${PYTHONPATH}}"
exec "${LOCAL_LLM_PYTHON:-python3}" -m local_llm_tools.sessions "$@"
