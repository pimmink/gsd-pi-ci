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
  case "${FAKE_CASE:-source}" in
    source) printf '%s\n' 'build  Error: registerRuntimeRead is not a function' ;;
    environment) printf '%s\n' 'test  native addon .node not found' ;;
    ref) printf '%s\n' 'verify  HEAD is not expected_sha' ;;
    policy) printf '%s\n' 'workflow  actionlint permission denied' ;;
    unknown) printf '%s\n' 'test  intermittent failure' ;;
  esac
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

for fixture in environment ref policy; do
  output="$(FAKE_CASE="$fixture" PATH="$fake_bin:$PATH" "$root_dir/scripts/remote-verify.sh" triage 42 --repo example/harness)"
  expected=""
  case "$fixture" in
    environment) expected=environment-or-native-staging ;;
    ref) expected=ref-or-base-mismatch ;;
    policy) expected=workflow-policy ;;
  esac
  printf '%s\n' "$output" | grep -q "\"classification\": \"$expected\""
done

output="$(FAKE_CASE=unknown PATH="$fake_bin:$PATH" "$root_dir/scripts/remote-verify.sh" triage 42 --repo example/harness)"
printf '%s\n' "$output" | grep -q '"classification": "unknown"'
printf '%s\n' "$output" | grep -q '"confidence": "low"'
printf 'remote-verify triage fixture passed\n'