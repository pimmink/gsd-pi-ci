# gsd-pi-ci

Fork-local CI/tooling harness for verifying `pimmink/gsd-pi` contributions on a clean,
free GitHub-hosted runner instead of a low-resource local laptop. This project is
**not** the `gsd-pi` fork itself, does not contain any `gsd-pi` source code, and does
not touch the current Copilot-catalogue branch or its PR diff in any way.

This repository is pushed to and lives at `pimmink/gsd-pi-ci` on GitHub, and its
workflows have been dispatched multiple times against real `pimmink/gsd-pi` commits
(see [`docs/phase-2-deferral.md`](docs/phase-2-deferral.md) for the run history). See
[`docs/remote-verification-guide.md`](docs/remote-verification-guide.md) for the full
operational manual (dispatching, watching, resuming, and interpreting a run), and
[`AGENTS.md`](AGENTS.md) for the short agent entry point.

## Purpose

`pimmink/gsd-pi`'s own local `verify:merge` wrapper (`scripts/verify-merge.sh`) is
currently missing the native test-engine build/staging step, which makes a fresh
checkout fail ~140 unit tests for reasons unrelated to the change being verified (see
[open-gsd/gsd-pi#1582](https://github.com/open-gsd/gsd-pi/issues/1582)). Combined with
resource contention on a low-core laptop, this makes local verification results hard to
trust. This harness runs the same `verify:pr` scope
(`build:core && typecheck:extensions && test:unit && gate:lifecycle-shadow-no-cutover`)
on a clean, dedicated 4-core runner, with the native engine correctly built and staged
first, so a "pr" signal from here is not confounded by either problem.

## Scope

- Two workflows, both remote `pr`-tier only:
  - `.github/workflows/remote-pr-verification.yml` — the stable, unsharded harness.
    Trusted fallback; always available; no baseline dependency of any kind.
  - `.github/workflows/remote-pr-verification-sharded.yml` — the generalized sharded
    harness. Derives its test manifest from whatever commit is actually checked out
    (never a hardcoded file/glob list or pass/fail/skip baseline), so it is safe to use
    against any branch regardless of test count. See
    [`docs/remote-verification-guide.md`](docs/remote-verification-guide.md) for
    tier-selection guidance.
- There is no `targeted` tier here — targeted feature tests and typechecking of
  touched files stay local, as before.
- There is no `merge`-tier workflow yet. `pimmink/gsd-pi`'s upstream CI also runs
  `windows-portability`, `node22-smoke`, and a conditional Docker e2e job as part of
  its blocking `build` gate; this harness does not reproduce any of those yet, so it is
  deliberately not named or presented as a `merge`-parity check. See
  [`docs/phase-2-deferral.md`](docs/phase-2-deferral.md).
- No Codespaces, no Copilot cloud agent usage anywhere in this design.

## What the remote `pr` run actually does

In this exact order, on `ubuntu-24.04`:

1. Verify `source_ref` currently resolves to `expected_sha` on `pimmink/gsd-pi`
   (`git ls-remote`, no objects fetched) — stop before touching anything if not.
2. Checkout that exact commit SHA (no token, no `actions/checkout`) and re-assert
   `HEAD` matches `expected_sha` — stop immediately if not.
3. `pnpm install --frozen-lockfile` (with the same install-time env flags
   `pimmink/gsd-pi`'s own `ci.yml` build job uses).
4. `pnpm run build:native:test` (dev + `--test-fault-injection` profile).
5. `pnpm run build:core`.
6. `pnpm run typecheck:extensions`.
7. `pnpm run test:compile`.
8. Mirror the built native addon into `dist-test/native/addon/` — the exact step
   `scripts/verify-merge.sh` currently omits.
9. `pnpm run test:unit:compiled` with `GSD_NATIVE_PREFER_LOCAL=1`, using the
   GitHub-hosted runner's own default `$HOME` (already fresh and per-run isolated —
   no separate `GSD_HOME` override; see "Security model" for why).
10. `pnpm run gate:lifecycle-shadow-no-cutover`, same env.

Every command and env flag above is copied verbatim from `pimmink/gsd-pi`'s own
`package.json` scripts and `.github/workflows/ci.yml` build job — nothing here is an
improvised alternative test pipeline.

## Security model

- `permissions: contents: read` at the workflow level; no elevated per-job permissions.
- No repository secrets are declared or referenced anywhere in the workflow.
- Checkout uses a manual, token-free `git init`/`fetch`/`checkout --force --detach`
  sequence (the same shape `pimmink/gsd-pi`'s own `ci.yml` uses to avoid
  `GITHUB_TOKEN`), retargeted at a pinned commit SHA in a different public repository.
  This never invokes `actions/checkout` and never holds a credential to begin with, so
  it exceeds a bare `persist-credentials: false` setting rather than needing one.
- The target repository (`pimmink/gsd-pi`) is hardcoded in the workflow; the only
  inputs a dispatch can supply are `source_ref` and `expected_sha`, both required, with
  no free-text shell/command input anywhere.
- `source_ref` and `expected_sha` are cross-checked against the real remote ref before
  checkout, and `HEAD` is re-checked against `expected_sha` immediately after checkout;
  either mismatch stops the run before any install/build/test step runs.
- Every third-party Action is pinned to a full commit SHA, not a floating tag. The
  `pnpm/action-setup`, `dtolnay/rust-toolchain`, and `Swatinem/rust-cache` SHAs are
  reused verbatim from `pimmink/gsd-pi`'s own `ci.yml`. `actions/setup-node` and
  `actions/upload-artifact` are additionally pinned here to their `v7.0.0` release
  commits (`820762786026740c76f36085b0efc47a31fe5020` and
  `bbbca2ddaa5d8feaa63e36b76fdaad77386f024f` respectively), even though upstream
  `ci.yml` currently references them by floating major-version tag, because this
  harness's own requirement is zero floating tags.
- `concurrency` is grouped per `source_ref` with `cancel-in-progress: true`, so
  re-dispatching against the same branch cancels a stale in-flight run.
- A hard `timeout-minutes: 60` bounds the job.
- No `GSD_HOME` override: an earlier revision pointed `GSD_HOME` at a second,
  job-scoped temp directory (`$RUNNER_TEMP/gsd-home`), separate from the runner's own
  `$HOME`. That diverged from what several upstream tests (`doctor-providers`,
  `app-smoke`, `rtk`) actually assert about `$HOME`-derived paths, causing 7 false-
  positive failures on the first real run. The GitHub-hosted runner's own `$HOME` is
  already a fresh, per-run-isolated filesystem, so no run ever touches real local
  state regardless — a second override was redundant and actively wrong.
- Every `run:` step executes as `bash --noprofile --norc -eo pipefail` (job-level
  `defaults.run.shell: bash`), so a real failure inside a piped command (e.g.
  `pnpm run test:unit:compiled 2>&1 | tee ...`) fails the step closed on `pnpm`'s exit
  code, not silently succeeds on `tee`'s. GitHub's implicit default shell (no `shell:`
  key at all) omits `pipefail`, which let the first real run's 9 real unit-test
  failures report as a green step/run — fixed by this explicit default.
- The Rust build cache uses its own namespaced key
  (`gsd-pi-ci-remote-pr-native-linux-x64-gnu`), distinct from `pimmink/gsd-pi ci.yml`'s
  own `native-linux-x64-gnu` key, so the two workflows can never cross-contaminate each
  other's cache. The pnpm dependency cache (via `actions/setup-node`'s `cache: pnpm`)
  is automatically namespaced per-repository by GitHub already.
- The job writes a compact `$GITHUB_STEP_SUMMARY` (repo/ref/sha/result). Full logs stay
  in the normal step logs. On failure only, the unit-test and lifecycle-gate log output
  is uploaded as a short-retention (3-day) diagnostic artifact — nothing is retained
  long-term.
- No tier here ever touches a live provider credential: the checked scope
  (`build:core`/`typecheck:extensions`/`test:unit`/lifecycle-shadow-gate) is
  deterministic and offline, inherited unchanged from `pimmink/gsd-pi`'s own local
  `verify:pr` scope.

## Operation

Manual `workflow_dispatch` only. There is no `push` or `pull_request` trigger, so this
workflow is structurally incapable of running on its own — it only ever runs when
someone explicitly dispatches it with a `source_ref` and `expected_sha`. It is intended
to be run at a stable local checkpoint after a branch has been pushed, not after every
small edit.

## Authorization boundaries (still standing)

The repository, its push history, and its past dispatches are no longer pending
authorization (all of that has already happened; see
[`docs/phase-2-deferral.md`](docs/phase-2-deferral.md) for the run history). The
following remain **not authorized by editing files in this repository** and each still
requires its own separate, explicit go-ahead:

- Opening any issue or pull request, on either `pimmink/gsd-pi` or `pimmink/gsd-pi-ci`.
- Force-pushing or deleting any branch.
- Merging anything upstream.
- Any change to historical/other in-flight branches this harness does not own.

## Run history

See [`docs/phase-2-deferral.md`](docs/phase-2-deferral.md) for the full, dated history
of every measured run (cold/invalid, valid baseline, sharding experiment, and the
generalized-workflow operationalization), including durations and evidence for every
claim made in this README.
