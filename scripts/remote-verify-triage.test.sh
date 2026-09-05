#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
fake_bin="$(mktemp -d)"
trap 'rm -rf "$fake_bin"' EXIT

cat > "$fake_bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"--json status,conclusion,url,jobs"* ]]; then
  printf '%s\n' '{"status":"completed","conclusion":"failure","url":"https://example.invalid/run/42","jobs":[{"name":"build","conclusion":"failure","url":"https://example.invalid/job/7"}]}'
elif [[ "$*" == *"--log-failed"* ]]; then
  printf '%s\n' 'build  Error: registerRuntimeRead is not a function'
else
  printf 'unexpected fake gh invocation: %s\n' "$*" >&2
  exit 1
fi
EOF
chmod +x "$fake_bin/gh"

output="$(PATH="$fake_bin:$PATH" "$root_dir/scripts/remote-verify.sh" triage 42 --repo example/harness)"
printf '%s\n' "$output" | grep -q '"classification": "source-or-test-contract"'
printf '%s\n' "$output" | grep -q '"firstCausalFailure": "build  Error: registerRuntimeRead is not a function"'
printf '%s\n' "$output" | grep -q '"confidence": "medium"'
printf 'remote-verify triage fixture passed\n'