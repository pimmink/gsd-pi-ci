# Phase 2: merge-parity tier (deferred) and sharding (proof complete, now operationalized)

This started as a deliberate scope note for a phase with nothing built yet. Sharding has
since moved from "deferred, no measurement" through "evidence-backed experiment" to
**"proof complete, and now operationalized as a branch-agnostic workflow"**: the original
sharding proof (run 32016308055) validated the shard-flag mechanics and the ~37% wall-clock
reduction against one calibration commit, using a hand-copied glob list and a hardcoded
expected file/pass/fail/skip baseline. That hardcoded baseline made the workflow silently
unusable against any other commit — discovered when a real unsharded reference run against
the `feat/github-copilot-model-catalog-sync` branch (run 32024561928) measured 14299 tests,
not the calibration commit's 14176. `.github/workflows/remote-pr-verification-sharded.yml`
has since been rewritten to derive its manifest from whatever commit is actually checked
out, removing the hardcoded-baseline dependency structurally (see "Operationalization" below
for the full explanation and the new run evidence). The `merge`-parity tier and Docker-e2e
items remain deferred, not yet started.

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

## Baseline drift discovered, and why operationalization was required (2026-08-17)

The experiment above proved sharding *works*, but its implementation encoded
point-in-time facts about one commit: 13 hand-copied test globs,
`EXPECTED_TEST_FILE_COUNT=1274`, and default totals of 14176/0/31. Those values are not
properties of the sharding mechanism — they are a snapshot of one specific tree. The
`feat/github-copilot-model-catalog-sync` branch legitimately has more tests than that
snapshot (new Copilot catalog tests added on top of upstream), so the experimental
workflow's fixed expectations would have hard-failed a completely healthy run for no
real reason. This is evidenced directly by comparing run 32024561928 (unsharded,
14299/0/31) against the experiment's hardcoded 14176/0/31 default — a mismatch that has
nothing to do with test correctness and everything to do with the workflow assuming its
calibration commit was permanent.

`.github/workflows/remote-pr-verification-sharded.yml` was rewritten so the canonical
test manifest is derived from the actually-checked-out commit's own `package.json`
`test:unit:compiled` script every run (never a copied glob list, never a hardcoded file
count), partitioned deterministically into a checksummed artifact all shards verify
identically, and reassembled in the aggregate job to prove structural coverage (no
gaps, no duplicates) instead of matching a magic number. Pass/skipped totals are summed
and reported dynamically; `failed` is asserted to be exactly zero, unconditionally. See
`docs/remote-verification-guide.md` for the full operational writeup of this design and
[`run 32024561928`](#reference-run-32024561928-catalog-sync-branch-unsharded-2026-08-17)
below for the reference run that exposed the original drift.

### Reference run: 32024561928 (catalog-sync branch, unsharded, 2026-08-17)

Dispatched against `feat/github-copilot-model-catalog-sync` at
`5e204ae7917aa9c73c806f293a979cf169889b6d` using the stable, unsharded
`remote-pr-verification.yml` (the sharded workflow was not used for this run — its
baseline was already known to be stale for this branch, so it would have failed the
file-count safety check before running any tests):

- **14299 passed, 0 failed, 31 skipped.**
- `gate:lifecycle-shadow-no-cutover` passed.
- Total wall-clock: ~15m53s.
- This is the reference total this session's generalized sharded workflow run is
  expected to match dynamically (not via a hardcoded comparison in the workflow
  itself — the workflow no longer encodes any expected total at all; this document is
  the only place the comparison is made, for human/agent evidence purposes).

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

## Design note: explicit manifest partitioning superseded `--test-shard` (2026-08-17)

The constraint above (verified during the original experiment) established that Node's
native `--test-shard` divides "all test files into total equal parts" deterministically
when given an identical file list. The generalized workflow still honors that
constraint's spirit but implements it more provably: instead of relying on each shard
process to independently re-resolve the same glob into the same list (which cannot be
directly observed to have happened identically across separate GitHub Actions runners),
the build job now computes the canonical manifest once, partitions it into explicit
per-shard files, checksums the whole set, and uploads it as a shared artifact every
shard downloads and verifies before running only its assigned partition. This is the
"explicit deterministic manifest partitioning" alternative anticipated as acceptable
when simpler and more provable — coverage is proven structurally (reassemble and diff
against the canonical list) rather than assumed from `--test-shard`'s documented
behavior alone.

## New runs from this session (2026-08-17, CI-harness operability work)

Filled in as each run in this session completes; see the session's final report for
the authoritative run IDs, URLs, totals, and durations.

