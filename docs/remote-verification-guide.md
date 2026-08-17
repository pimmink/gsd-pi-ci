# Remote verification guide (canonical)

This is the canonical operational manual for `gsd-pi-ci`. Read this before dispatching,
watching, or changing anything here. `AGENTS.md` is a short entry point that points here
— it does not duplicate this content.

## The verification tiers, and when to use which

| Tier | Where | Scope | Typical time | Authority |
|---|---|---|---|---|
| Local targeted loop | your laptop | only tests touching changed source (`pnpm run test:changed:src`) plus `typecheck:extensions` | seconds–minutes | fast iteration signal, not a merge gate |
| Local `verify:pr` | your laptop | full `build:core && typecheck:extensions && test:unit && gate:lifecycle-shadow-no-cutover` | ~15–25 min, contends with local CPU/thermal limits | a personal pre-push sanity check, not authoritative — this repo's own history includes false failures caused by local resource contention (`ETIMEDOUT` fault-harness timeouts) that a clean runner does not reproduce |
| Stable remote unsharded (`remote-pr-verification.yml`, this repo) | GitHub-hosted `ubuntu-24.04` runner, dispatched from `pimmink/gsd-pi-ci` | same scope as local `verify:pr`, run on a clean 4-core runner | ~15–20 min | trusted fallback; always available; zero baseline dependency |
| Generalized sharded remote (`remote-pr-verification-sharded.yml`, this repo) | same runner class, 4 (configurable) parallel shards | same scope, split across shards for wall-clock reduction | ~10–13 min end-to-end | primary remote tier once a branch is stable enough to push; branch/test-count agnostic |
| Real upstream PR/merge CI (`pimmink/gsd-pi`'s own `.github/workflows/ci.yml`, after a PR is opened) | GitHub-hosted runners under `open-gsd/gsd-pi` or `pimmink/gsd-pi` | the actual blocking gate: `build` (Linux), `windows-portability`, `node22-smoke`, conditional Docker e2e | varies | **the only authoritative merge signal** — nothing in this repo is a substitute for it |

Nothing in this repository is merge-parity. `windows-portability`, `node22-smoke`, and
the conditional Docker e2e job remain out of scope for both workflows here — see
`docs/phase-2-deferral.md`.

## Why the laptop is never used for real parallel shards

This harness exists because a low-resource local laptop produces false failures under
resource contention (documented `ETIMEDOUT` fault-harness timeouts in this repo's own
run history) that a clean, dedicated GitHub-hosted runner does not. Running multiple
real shards' worth of the ~14k-test suite concurrently on the same laptop would
reintroduce exactly that problem — worse, since real parallel shards contend with each
other on the same CPU instead of a single sequential run contending with the rest of
the machine. Local shard/manifest validation therefore always uses a tiny synthetic
fixture (a handful of near-instant throwaway tests), run sequentially, never the real
suite and never multiple real shards concurrently. See `docs/phase-2-deferral.md`'s
"Constraints that continue to apply" section for the specific fixture validation this
was based on.

## Native-addon staging

Both workflows build the native Rust addon with the dev + fault-injection profile
(`pnpm run build:native:test`) before running any tests, then mirror the compiled
`.node` file into `dist-test/native/addon/` — the path the compiled-test loader
actually reads from. Skipping or misordering this step is a well-known local failure
mode (missing addon → native-dependent tests fail or hang); both workflows fail closed
with an explicit `::error::` if no `.node` file is found after the build step, rather
than silently continuing without it.

## `pipefail`

Every `run:` step in both workflows either sets `set -o pipefail` (or the stricter
`set -euo pipefail`) explicitly, or runs under a job-level `defaults.run.shell: bash`
combined with an explicit `pipefail` in the step that pipes test output through `tee`.
This repository's own history includes a real incident where a missing `pipefail` on a
`... | tee log.txt` pipeline caused 9 real unit-test failures to be silently masked
behind `tee`'s own (successful) exit code — the workflow reported green while tests
were actually failing. Never add a new piped step without `pipefail` active in that
step's shell.

## The `GSD_HOME` lesson

An early cold run's `GSD_HOME` override caused 7 of 9 masked test failures (see above)
to fail for a reason entirely unrelated to the code being verified — the override
pointed at a path the fault-harness didn't expect, producing false failures. Both
workflows here deliberately do not set `GSD_HOME` at all; they rely on the runner's
default state. If a future change needs to set `GSD_HOME`, treat it as a change that
requires its own explicit verification run before trusting any pass/fail signal that
followed it, precisely because this exact mistake has silently produced false failures
here before.

## Exact SHA pinning (never guess a SHA)

Both workflows require `source_ref` (a plain branch name) and `expected_sha` (a full
40-character lowercase hex commit SHA) as `workflow_dispatch` inputs, and both verify —
via `git ls-remote` against the live repo, before touching any objects — that
`source_ref` currently resolves to exactly `expected_sha` on `pimmink/gsd-pi`. If they
don't match, the run fails immediately with a clear `::error::`, before checkout. After
checkout, a second step re-asserts `git rev-parse HEAD == expected_sha`, so a race
between the preflight check and the actual fetch can never silently verify the wrong
commit. `scripts/remote-verify.sh dispatch` performs the same `git ls-remote` check
locally before ever calling `gh workflow run`, so a mismatched ref/SHA pair is rejected
before a run is even created. Never shorten, truncate, or guess a SHA for a dispatch —
always resolve it fresh with `git rev-parse HEAD` or `git ls-remote`.

## Visibility, watching, status, and resuming

Both workflows declare a top-level `run-name:` that embeds the mode (`Stable verify:` /
`Sharded verify:`), `source_ref`, `expected_sha`, and the GitHub run number, so a run is
immediately distinguishable from any other run in the Actions list without opening it.
Both also write a job summary at the very start (source repo, source_ref, expected_sha,
actual checked-out SHA, workflow version/ref, shard count where applicable, and a
direct run URL) so an in-progress run is inspectable from the Summary tab alone, and a
final summary at completion.

`scripts/remote-verify.sh` wraps dispatch/status/watch/resume/open/logs so no one has to
remember raw `gh` invocations:

```bash
# Dispatch and follow live (stable tier)
scripts/remote-verify.sh dispatch --mode stable --source-ref my-branch \
  --expected-sha <full-40-char-sha>
scripts/remote-verify.sh watch <run-id>

# Dispatch and follow live (generalized sharded tier, default 4 shards)
scripts/remote-verify.sh dispatch --mode sharded --source-ref my-branch \
  --expected-sha <full-40-char-sha> --shard-count 4
scripts/remote-verify.sh watch <run-id>

# Check status once, without blocking (compact job table)
scripts/remote-verify.sh status <run-id>

# Resume watching after a lost terminal, using only the run ID
scripts/remote-verify.sh resume <run-id>

# Open the run in a browser
scripts/remote-verify.sh open <run-id>

# Get final conclusion, duration, per-job status, and real pass/fail/skip totals
# (plus log/artifact locations if it failed)
scripts/remote-verify.sh logs <run-id>
```

`watch`/`resume` poll at a fixed interval but only print a new report when the
job/status snapshot actually changes, so output stays compact during a long run while
never leaving more than ~15s of silence during genuine status changes. `logs` never
trusts the green checkmark alone — it greps the real compact-reporter/aggregate summary
line (`✔ N passed, N failed, N skipped` or `Aggregate: N passed, N failed, N skipped`)
out of the actual job logs before reporting totals. No subcommand ever prints a token,
credential, or other auth output.

## Fallback behavior

`remote-pr-verification.yml` (the stable, unsharded tier) is never modified by sharding
work and carries zero baseline dependency of any kind — it always re-derives its own
pass/fail signal from the actual run, with no expected file count or historical total
anywhere in it. If the generalized sharded workflow ever has a real bug, fall back to
dispatching the stable tier while the sharded workflow is fixed; never present a passing
stable-tier run as proof that a broken sharded workflow is "fine", and never present a
passing sharded run as a substitute for real upstream PR/merge CI.

## Known limitations

- Not merge-parity: `windows-portability`, `node22-smoke`, and the conditional Docker
  e2e job are out of scope for both workflows here (see `docs/phase-2-deferral.md`).
- The sharded workflow's per-shard dependency install is re-run per shard (cache-backed,
  cheap) rather than packaged as part of the shared build artifact — see
  `docs/phase-2-deferral.md` for the measured cost of this tradeoff.
- `lifecycle-gate` runs once (not sharded) in the sharded workflow, since it is not a
  per-test-file check and sharding it would provide no benefit.
- Dispatch is limited to `pimmink/gsd-pi` as the only verifiable target repo; both
  workflows hardcode `SOURCE_REPO`/`repo_url` rather than accepting an arbitrary
  attacker-controlled repo input, by design — this is a security boundary, not an
  oversight.

## Which tier an agent should use, and when

1. While actively iterating on a change: local targeted loop
   (`pnpm run test:changed:src` + `typecheck:extensions`) only.
2. Once a change is stable and about to be pushed: dispatch the generalized sharded
   tier here for a fast, clean-runner signal.
3. If the sharded workflow itself is suspected of a harness bug: dispatch the stable
   unsharded tier here as a trusted cross-check.
4. Before actually merging anything upstream: open the real PR and rely on
   `pimmink/gsd-pi`'s own upstream CI as the only authoritative merge signal. Nothing
   in this repository is a substitute for that.
5. Never run the full ~14k-test suite, or more than one real shard's worth of it,
   directly on a local laptop — see "Why the laptop is never used for real parallel
   shards" above.
