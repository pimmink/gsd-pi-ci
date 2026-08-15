# Phase 2 (deferred): merge-parity tier and sharding

This is a deliberate scope note, not an implementation. Nothing described here is built
yet, and none of it should be started before the condition below is met.

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
- **Sharding of `test:unit:compiled`.** No measurement of remote wall-clock time exists
  yet for a single, unsharded run on one `ubuntu-24.04` (4-core) runner. Designing a
  sharding scheme before that measurement exists would be guessing at a problem that
  may not exist, or sizing a solution to the wrong bottleneck.
- **Docker-e2e parity.** Not attempted until the two items above are addressed, since it
  is one of upstream `build`'s own conditional steps and belongs in the same
  parity-tracking effort as `windows-portability`/`node22-smoke`.

## Condition to revisit

Do not start phase 2 work until at least one real, measured run of
`remote-pr-verification.yml` has completed on the actual `ubuntu-24.04` runner and its
wall-clock time (and pass/fail outcome) has been recorded. That measurement is the
input phase 2 needs to decide:

### First cold run recorded (run 31854731553, 2026-08-15) — not sufficient on its own

- Total workflow duration: 20m56s; unit-test step alone: ~15m31s.
- Native addon build/staging worked correctly.
- No timeout- or resource-exhaustion signals.
- This run was formally invalid (masked test failures via a missing `pipefail`, plus
  7 false-positive failures from a since-removed `GSD_HOME` override) — both fixed in
  a follow-up revision. Do not use this run's numbers alone to decide sharding; wait
  for a second, cache-warm, actually-valid run's measurement first, since a cold run
  (empty Rust/pnpm caches) is not representative of steady-state wall-clock time.

1. Whether a `merge`-parity tier is even feasible/worthwhile on a single unsharded
   runner, or whether sharding must be designed first to keep it inside a reasonable
   timeout.
2. Which additional upstream jobs (`windows-portability`, `node22-smoke`, conditional
   Docker e2e) are worth reproducing here at all, versus continuing to rely on real
   upstream CI (post-`Ship.`) as the source of truth for those specific checks.

## Constraints that continue to apply once phase 2 starts

- Any sharding design must build and compile tests exactly once, with shards
  downloading the same pre-built artifact rather than each shard rebuilding.
- Shard-to-file assignment must be deterministic (stable hashing over the same file
  list `test:unit:compiled` already globs), so bucket membership does not change
  between runs unless the underlying file list changes.
- Sharding must never be dry-run by starting multiple concurrent shard processes on a
  local low-resource machine — that reintroduces exactly the local resource-contention
  problem this whole effort exists to move away from. Any local experimentation must
  stay at the level of static config/schema review, not concurrent local execution.
