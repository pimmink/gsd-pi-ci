#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
node --test "$root_dir"/scripts/__tests__/*.test.mjs
bash "$root_dir/scripts/remote-verify-triage.test.sh"