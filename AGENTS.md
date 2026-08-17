# AGENTS.md

Short entry point for any agent working in `gsd-pi-ci`. Read
[`docs/remote-verification-guide.md`](docs/remote-verification-guide.md) first — it is
the canonical operational manual and is not duplicated here.

## Required behavior for any agent working in this repo

- Read `docs/remote-verification-guide.md` before dispatching, watching, or changing
  either workflow.
- Keep targeted tests and typechecking local. Only use this repo's workflows for the
  remote `pr`-tier signal; never attempt real sharded execution on a local machine.
- Never guess, truncate, or shorten a commit SHA. Always resolve the current SHA with
  `git rev-parse HEAD` or `git ls-remote` immediately before a dispatch, and let both
  workflows' own pre-checkout `git ls-remote` verification confirm it independently.
- Report the run URL immediately after dispatching, and report active status regularly
  while a run is in progress (see the guide's visibility/watch section) — never leave a
  long silent gap while a run is running.
- Never trust a green checkmark alone. Confirm real pass/fail/skip totals from the
  actual job logs (`scripts/remote-verify.sh logs <run-id>`) before treating a run as
  proof of anything.
- Never treat a hardcoded historical test total (from this file, the guide, or
  `docs/phase-2-deferral.md`) as current truth for a different commit. The sharded
  workflow derives its own manifest and totals fresh from whatever commit is actually
  checked out — always compare against a freshly dispatched run's own real output, not
  a remembered number.
- Any change to either workflow file must be accompanied by an update to
  `docs/remote-verification-guide.md` in the same change, so the guide never drifts
  from what the workflows actually do.
