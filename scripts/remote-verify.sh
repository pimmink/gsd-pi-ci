#!/usr/bin/env bash
# gsd-pi-ci: dispatch/status/watch/resume helper for the remote verification workflows.
#
# Wraps `gh workflow run` + `gh run view`/`gh run list` so that dispatching a remote
# verification run always: (1) proves source_ref/expected_sha match on the real remote
# before dispatching (never guesses a SHA), (2) deterministically recovers the exact
# run ID GitHub assigned (gh workflow run does not return one), (3) prints a clickable
# URL and resume commands immediately, and (4) can be re-attached to after a lost
# terminal using only that run ID. Never prints tokens, auth output, or other secrets.
#
# Usage:
#   scripts/remote-verify.sh dispatch --mode stable|sharded --source-ref <branch> \
#       --expected-sha <40-char-sha> [--shard-count N] [--workflow-ref <ref>] \
#       [--target-repo <owner/repo>] [--repo <owner/repo>]
#   scripts/remote-verify.sh status <run-id> [--repo <owner/repo>]
#   scripts/remote-verify.sh watch <run-id> [--repo <owner/repo>]
#   scripts/remote-verify.sh resume <run-id> [--repo <owner/repo>]   # alias for watch
#   scripts/remote-verify.sh open <run-id> [--repo <owner/repo>]
#   scripts/remote-verify.sh logs <run-id> [--repo <owner/repo>]     # final totals + failure log locations
#
# See docs/remote-verification-guide.md for the full operational writeup.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
HARNESS_REPO="pimmink/gsd-pi-ci"     # where the workflows live and are dispatched from
TARGET_REPO="pimmink/gsd-pi"          # the repo whose commit is being verified
STABLE_WORKFLOW="remote-pr-verification.yml"
SHARDED_WORKFLOW="remote-pr-verification-sharded.yml"
POLL_INTERVAL_SECS=15
DISPATCH_WAIT_TIMEOUT_SECS=60

die() {
  echo "::error::$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found on PATH"
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

# --- arg parsing helpers -----------------------------------------------------

MODE=""
SOURCE_REF=""
EXPECTED_SHA=""
SHARD_COUNT="4"
WORKFLOW_REF="main"
RUN_ID=""

parse_dispatch_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode) MODE="$2"; shift 2 ;;
      --source-ref) SOURCE_REF="$2"; shift 2 ;;
      --expected-sha) EXPECTED_SHA="$2"; shift 2 ;;
      --shard-count) SHARD_COUNT="$2"; shift 2 ;;
      --workflow-ref) WORKFLOW_REF="$2"; shift 2 ;;
      --target-repo) TARGET_REPO="$2"; shift 2 ;;
      --repo) HARNESS_REPO="$2"; shift 2 ;;
      *) die "unknown argument to dispatch: $1" ;;
    esac
  done

  [[ "$MODE" == "stable" || "$MODE" == "sharded" ]] || die "--mode must be 'stable' or 'sharded', got: ${MODE:-<empty>}"
  [[ -n "$SOURCE_REF" ]] || die "--source-ref is required"
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || die "--expected-sha must be a full 40-character lowercase hex SHA, got: ${EXPECTED_SHA:-<empty>}"
  [[ "$SHARD_COUNT" =~ ^[0-9]+$ ]] || die "--shard-count must be an integer"
}

parse_run_id_and_repo() {
  RUN_ID="${1:-}"
  shift || true
  [[ -n "$RUN_ID" ]] || die "a run-id argument is required"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) HARNESS_REPO="$2"; shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
}

# --- dispatch -----------------------------------------------------------------

cmd_dispatch() {
  parse_dispatch_args "$@"

  local workflow_file
  if [[ "$MODE" == "stable" ]]; then
    workflow_file="$STABLE_WORKFLOW"
  else
    workflow_file="$SHARDED_WORKFLOW"
  fi

  # Never guess a SHA: prove source_ref currently resolves to expected_sha on the
  # real remote before touching gh workflow run at all.
  echo "Verifying ${SOURCE_REF} on ${TARGET_REPO} currently resolves to ${EXPECTED_SHA} ..."
  local remote_line remote_sha
  remote_line="$(git ls-remote "https://github.com/${TARGET_REPO}.git" "refs/heads/${SOURCE_REF}")" \
    || die "git ls-remote failed for ${SOURCE_REF} on ${TARGET_REPO}"
  [[ -n "$remote_line" ]] || die "refs/heads/${SOURCE_REF} does not exist on ${TARGET_REPO}"
  remote_sha="$(printf '%s\n' "$remote_line" | cut -f1)"
  [[ "$remote_sha" == "$EXPECTED_SHA" ]] || die "refs/heads/${SOURCE_REF} on ${TARGET_REPO} currently points at ${remote_sha}, not ${EXPECTED_SHA}. Refusing to dispatch on a mismatched ref/sha pair."
  echo "Confirmed: ${SOURCE_REF} == ${EXPECTED_SHA} on ${TARGET_REPO}."

  # Snapshot the most recent run for this workflow so the post-dispatch run can be
  # found deterministically (gh workflow run does not return a run ID).
  local before_id
  before_id="$(gh run list --repo "$HARNESS_REPO" --workflow "$workflow_file" --limit 1 --json databaseId --jq '.[0].databaseId // "none"' 2>/dev/null || echo none)"

  echo "Dispatching ${workflow_file} (workflow-ref=${WORKFLOW_REF}) against ${HARNESS_REPO} ..."
  if [[ "$MODE" == "stable" ]]; then
    gh workflow run "$workflow_file" --repo "$HARNESS_REPO" --ref "$WORKFLOW_REF" \
      -f "source_ref=${SOURCE_REF}" -f "expected_sha=${EXPECTED_SHA}"
  else
    gh workflow run "$workflow_file" --repo "$HARNESS_REPO" --ref "$WORKFLOW_REF" \
      -f "source_ref=${SOURCE_REF}" -f "expected_sha=${EXPECTED_SHA}" -f "shard_count=${SHARD_COUNT}"
  fi

  echo "Waiting for GitHub to register the new run (up to ${DISPATCH_WAIT_TIMEOUT_SECS}s) ..."
  local waited=0 found_id="none"
  while [[ "$waited" -lt "$DISPATCH_WAIT_TIMEOUT_SECS" ]]; do
    sleep 3
    waited=$((waited + 3))
    found_id="$(gh run list --repo "$HARNESS_REPO" --workflow "$workflow_file" --limit 1 --json databaseId --jq '.[0].databaseId // "none"' 2>/dev/null || echo none)"
    if [[ "$found_id" != "none" && "$found_id" != "$before_id" ]]; then
      break
    fi
    found_id="none"
  done
  [[ "$found_id" != "none" ]] || die "could not deterministically find the new run ID within ${DISPATCH_WAIT_TIMEOUT_SECS}s. Run 'gh run list --repo ${HARNESS_REPO} --workflow ${workflow_file}' manually."

  local run_url
  run_url="$(gh run view "$found_id" --repo "$HARNESS_REPO" --json url --jq '.url')"

  echo ""
  echo "=== Run dispatched ==="
  echo "run-id:       ${found_id}"
  echo "run-url:      ${run_url}"
  echo "source-ref:   ${SOURCE_REF}"
  echo "tested-sha:   ${EXPECTED_SHA}"
  echo "workflow-ref: ${WORKFLOW_REF} (${workflow_file})"
  echo ""
  echo "Resume commands:"
  echo "  ${SCRIPT_NAME} status ${found_id} --repo ${HARNESS_REPO}"
  echo "  ${SCRIPT_NAME} watch  ${found_id} --repo ${HARNESS_REPO}"
  echo "  ${SCRIPT_NAME} open   ${found_id} --repo ${HARNESS_REPO}"
  echo "  ${SCRIPT_NAME} logs   ${found_id} --repo ${HARNESS_REPO}"
}

# --- status (compact job table, one-shot) -------------------------------------

cmd_status() {
  parse_run_id_and_repo "$@"
  gh run view "$RUN_ID" --repo "$HARNESS_REPO"
}

# --- watch (only reports on status changes) -----------------------------------

cmd_watch() {
  parse_run_id_and_repo "$@"
  local run_url
  run_url="$(gh run view "$RUN_ID" --repo "$HARNESS_REPO" --json url --jq '.url')"
  echo "Watching run ${RUN_ID} (${run_url}) — reporting on every status change, at least every ${POLL_INTERVAL_SECS}s."

  local last_snapshot=""
  while true; do
    local snapshot status conclusion
    snapshot="$(gh run view "$RUN_ID" --repo "$HARNESS_REPO" --json status,conclusion,jobs \
      --jq '{status, conclusion, jobs: [.jobs[] | {name, status, conclusion}]}')"
    status="$(echo "$snapshot" | node -e "process.stdout.write(JSON.parse(require('fs').readFileSync(0,'utf8')).status)")"
    conclusion="$(echo "$snapshot" | node -e "const j=JSON.parse(require('fs').readFileSync(0,'utf8')); process.stdout.write(j.conclusion||'')")"

    if [[ "$snapshot" != "$last_snapshot" ]]; then
      echo ""
      echo "[$(date -u +%H:%M:%S)] status=${status} conclusion=${conclusion:-<pending>}"
      echo "$snapshot" | node -e "
        const j = JSON.parse(require('fs').readFileSync(0,'utf8'));
        for (const job of j.jobs) {
          console.log('  - ' + job.name + ': ' + job.status + (job.conclusion ? ' (' + job.conclusion + ')' : ''));
        }
      "
      last_snapshot="$snapshot"
    fi

    if [[ "$status" == "completed" ]]; then
      break
    fi
    sleep "$POLL_INTERVAL_SECS"
  done

  echo ""
  echo "=== Run completed: conclusion=${conclusion} ==="
  cmd_logs "$RUN_ID" --repo "$HARNESS_REPO"
}

cmd_resume() {
  cmd_watch "$@"
}

# --- open (browser) ------------------------------------------------------------

cmd_open() {
  parse_run_id_and_repo "$@"
  gh run view "$RUN_ID" --repo "$HARNESS_REPO" --web
}

# --- logs (final totals + failure locations) ------------------------------------

cmd_logs() {
  parse_run_id_and_repo "$@"
  local meta duration_s created updated conclusion
  meta="$(gh run view "$RUN_ID" --repo "$HARNESS_REPO" --json conclusion,status,createdAt,updatedAt,jobs,url)"
  conclusion="$(echo "$meta" | node -e "process.stdout.write(JSON.parse(require('fs').readFileSync(0,'utf8')).conclusion||'')")"
  created="$(echo "$meta" | node -e "process.stdout.write(JSON.parse(require('fs').readFileSync(0,'utf8')).createdAt)")"
  updated="$(echo "$meta" | node -e "process.stdout.write(JSON.parse(require('fs').readFileSync(0,'utf8')).updatedAt)")"
  duration_s=$(( $(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$updated" +%s 2>/dev/null || date -u -d "$updated" +%s) \
               - $(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$created" +%s 2>/dev/null || date -u -d "$created" +%s) ))

  echo "conclusion: ${conclusion}"
  echo "duration:   ${duration_s}s"
  echo "url:        $(echo "$meta" | node -e "process.stdout.write(JSON.parse(require('fs').readFileSync(0,'utf8')).url)")"
  echo ""
  echo "per-job status:"
  echo "$meta" | node -e "
    const j = JSON.parse(require('fs').readFileSync(0,'utf8'));
    for (const job of j.jobs) {
      console.log('  - ' + job.name + ': ' + job.status + (job.conclusion ? ' (' + job.conclusion + ')' : ''));
    }
  "

  echo ""
  echo "real pass/fail/skip totals (parsed from job logs, not just the checkmark):"
  # Stable workflow: one 'Run unit tests' step. Sharded workflow: an 'aggregate' job
  # step prints 'Aggregate: N passed, N failed, N skipped'. Try both patterns.
  local totals
  totals="$(gh run view "$RUN_ID" --repo "$HARNESS_REPO" --log 2>/dev/null \
    | grep -E '✔ [0-9]+ passed, [0-9]+ failed, [0-9]+ skipped|Aggregate: [0-9]+ passed, [0-9]+ failed, [0-9]+ skipped' \
    || true)"
  if [[ -n "$totals" ]]; then
    echo "$totals"
  else
    echo "  (no summary line found yet — run may still be in progress, or failed before producing one)"
  fi

  if [[ "$conclusion" != "success" && -n "$conclusion" ]]; then
    echo ""
    echo "Run did not succeed. Relevant locations:"
    echo "  - full logs:      gh run view ${RUN_ID} --repo ${HARNESS_REPO} --log"
    echo "  - failed steps:   gh run view ${RUN_ID} --repo ${HARNESS_REPO} --log-failed"
    echo "  - diagnostics artifact (if uploaded): gh run download ${RUN_ID} --repo ${HARNESS_REPO}"
    echo "  - browser:        gh run view ${RUN_ID} --repo ${HARNESS_REPO} --web"
  fi
}

# --- main ------------------------------------------------------------------------

main() {
  require_cmd gh
  require_cmd git
  require_cmd node

  local sub="${1:-}"
  shift || true
  case "$sub" in
    dispatch) cmd_dispatch "$@" ;;
    status) cmd_status "$@" ;;
    watch) cmd_watch "$@" ;;
    resume) cmd_resume "$@" ;;
    open) cmd_open "$@" ;;
    logs) cmd_logs "$@" ;;
    -h|--help|"") usage ;;
    *) die "unknown subcommand: $sub (expected: dispatch|status|watch|resume|open|logs)" ;;
  esac
}

main "$@"
