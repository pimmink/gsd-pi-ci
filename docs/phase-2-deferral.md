# Phase 2: merge-parity tier (deferred) and sharding (evidence-backed, in progress)

This started as a deliberate scope note for a phase with nothing built yet. Sharding has
since moved from "deferred, no measurement" to "evidence-backed, in progress": two real
runs of `remote-pr-verification.yml` (one cold, one cache-warm and valid) have measured
the actual bottleneck, and `.github/workflows/remote-pr-verification-sharded.yml` is an
experimental workflow validating a sharded design against that measurement. The
`merge`-parity tier and Docker-e2e items remain deferred, not yet started.

## What is deferred and why

Phase 1 ships exactly one tier: remote `pr`-verification
(`.github/workflows/remote-pr-verification.yml`), matching `pimmink/gsd-pi`'s local
`verify:pr` scope plus the native-engine build/staging fix. It deliberately does not
include:

- **A `merge`-parity tier.** `pimmink/gsd-pi`'s upstream `ci.yml` blocking gate is not
  just its `build` job. The full gate (`ci-gate`) also requires `windows-portability`
  (Windows-native file-identity/directory-sync tests, conditional on
  portability/windows-e2e paths changing) and `node22-smoke` (the declared Node engines
  floor) to pass or be legitimately skipped, and `build` itself conditionally runs a
  Docker e2e suite. A harness tier that only reproduces the Linux `build` job's steps is
  not full parity with what actually blocks a merge upstream, so it must not be called
  `merge` or presented as merge-equivalent until those additional jobs (or an explicit,
  documented decision to intentionally omit some of them) are part of the design.
- **Sharding of `test:unit:compiled`.** Evidence-backed and in progress (no longer
  purely deferred) — see "Condition to revisit" below for the two measured runs that
  informed this, and `.github/workflows/remote-pr-verification-sharded.yml` for the
  experimental implementation.
- **Docker-e2e parity.** Not attempted until the two items above are addressed, since it
  is one of upstream `build`'s own conditional steps and belongs in the same
  parity-tracking effort as `windows-portability`/`node22-smoke`.

## Condition to revisit

Phase 2 required at least one real, measured run of `remote-pr-verification.yml` on the
actual `ubuntu-24.04` runner, with wall-clock time and pass/fail outcome recorded, and a
second cache-warm run to confirm the first cold run's numbers were representative. Both
conditions are now met (see below), so the sharding sub-item below is evidence-backed
and in progress; see `.github/workflows/remote-pr-verification-sharded.yml`. The
`merge`-parity and Docker-e2e items remain deferred until sharding's own results land.

### First cold run recorded (run 31854731553, 2026-08-15) — not sufficient on its own

- Total workflow duration: 20m56s; unit-test step alone: ~15m31s.
- Native addon build/staging worked correctly.
- No timeout- or resource-exhaustion signals.
- This run was formally invalid (masked test failures via a missing `pipefail`, plus
  7 false-positive failures from a since-removed `GSD_HOME` override) — both fixed in
  a follow-up revision. Do not use this run's numbers alone to decide sharding; wait
  for a second, cache-warm, actually-valid run's measurement first, since a cold run
  (empty Rust/pnpm caches) is not representative of steady-state wall-clock time.

### Second, valid, cache-warm run recorded (run 31857969918) — satisfies the condition

- Total workflow duration: 19m11s.
- `pnpm install` (cache-warm): ~11.8s. Native addon build (Rust cache-warm): ~9.15s.
  Both were confirmed cache hits, not cold builds — caching itself works correctly.
- `test:unit:compiled` alone: ~15m42s — this step is the actual bottleneck, not
  dependency installation or the native build.
- `pipefail` was active for this run (fixed after the first cold run), so this
  measurement is trustworthy and not masking hidden failures.
- Conclusion: caching is not the problem. The unit-test suite's own single-runner,
  unsharded wall-clock time is what phase 2's sharding sub-item targets.

Remaining open items for phase 2, now that sharding is evidence-backed and in progress:

1. Whether a `merge`-parity tier is even feasible/worthwhile on a single unsharded
   runner, or whether sharding results change that calculus once measured.
2. Which additional upstream jobs (`windows-portability`, `node22-smoke`, conditional
   Docker e2e) are worth reproducing here at all, versus continuing to rely on real
   upstream CI (post-`Ship.`) as the source of truth for those specific checks.

## Sharding experiment result (run 32016308055, 2026-08-17)

`.github/workflows/remote-pr-verification-sharded.yml`, dispatched against
`fix/mai-cost-table-provider-section` at `28a2c92c3835e61f5fd19beffc6b8b4c6476bfc6`
with `shard_count=4`:

- All jobs succeeded: `build`, 4/4 `test-shard` matrix jobs, `lifecycle-gate`,
  `aggregate`.
- Aggregate totals across the 4 shards: **14176 passed, 0 failed, 31 skipped** — an
  exact match to the known unsharded baseline for this commit. Per-shard breakdown:
  shard 1: 3847/0/6, shard 2: 3148/0/4, shard 3: 3700/0/3, shard 4: 3481/0/18. This is
  conclusive proof no test file was skipped or run twice: any gap or duplicate would
  have changed the summed total.
- Compiled test file count check passed against the corrected baseline of 1274 (see
  the workflow's `EXPECTED_TEST_FILE_COUNT` comment for why the first dispatch's
  1272 guess was wrong and how 1274 was derived).
- `gate:lifecycle-shadow-no-cutover` passed, running once (not sharded), in 1m21s.
- `pipefail` was active throughout (job-level `defaults.run.shell: bash`).
- Total wall clock: build 4m22s + longest shard (1/4) 7m35s + aggregate 7s ≈ **12m04s**
  end-to-end, run started to finished, versus the 19m11s unsharded baseline (run
  31857969918) — a **~37% reduction**, comfortably above the ~30% target and consistent
  with the estimate that motivated starting at 4 shards rather than 2.
- A first dispatch (run 32015752880) failed fast and cheaply at the file-count safety
  check (1274 actual vs. an incorrectly hand-counted 1272 expected) before any shard
  ran — this is the safety check performing exactly as designed, not a sharding
  correctness problem. Fixed and re-dispatched once (2 dispatches total, as planned).

This confirms the shard-flag placement and correctness proof are now demonstrated in
a real remote CI run, not only in the local synthetic-fixture validation above.

## Constraints that continue to apply once phase 2 starts

- Any sharding design must build and compile tests exactly once, with shards
  downloading the same pre-built artifact rather than each shard rebuilding. Satisfied
  by the experimental workflow's `build` job + shared artifact download.
- Shard-to-file assignment must be deterministic, so bucket membership does not change
  between runs unless the underlying file list changes. The experimental workflow uses
  Node's native `--test-shard=<index>/<total>` (not a custom stable-hash manifest) —
  this was verified locally before adoption, using a synthetic multi-glob fixture with
  the exact production flag combination (`--experimental-test-isolation=process`, the
  custom compact reporter, shard flag placed immediately after `--test`): two full runs
  produced byte-identical per-shard file sets, zero duplicates, and zero omissions,
  including with an uneven file-count/shard-count ratio. Node's own documentation
  states the mechanism divides "all test files into total equal parts" — the file list
  is the unit of division, so completeness and non-overlap are guaranteed by
  construction, not by chance. One caveat found during that same validation: the shard
  flag must be placed before the glob patterns on the command line — placing it after
  silently disables partitioning (every shard reran the full file set).
- Sharding must never be dry-run by starting multiple concurrent shard processes on a
  local low-resource machine — that reintroduces exactly the local resource-contention
  problem this whole effort exists to move away from. Any local experimentation must
  stay at the level of static config/schema review, not concurrent local execution.
  (The synthetic-fixture validation above used a single tiny throwaway test set with
  trivial, near-instant tests — not the real ~1,272-file suite, and never running
  multiple shards' worth of real work concurrently on this machine.)

