# Native cmux artifact route (D014 / D015 / actual D019)

> [!WARNING]
> **Uncommitted preparation: independent review required before commit or execution.**
> Actual D019 permits the unchanged stock tagged route and its app/helper startup
> on the temporary CI runner. It also permits a **separate** resulting Intel testapp
> on macOS **15.7.9**, only after separate safe admission; it does not authorize this
> preparation pass to start anything. No lifecycle patch solely to avoid startup is
> needed. Active cmux/sessions remain untouched. Static mocks are not admission.
> Review the full fixed CI code, setup and stock control flow before any execution.
> Accounting for the original remaining budget and actual dependency compatibility
> also remain gates. No build, dispatch or native-acceptance success is claimed.

This **separate, manual-only** candidate permits at most one authorized native build
attempt, followed by non-executing artifact checks. It is not GSD verification,
local product installation, local app/PTY launch, live OS acceptance, role handover, or a release channel.
The historical workflow filename/run title retain `build-only` for queue-identity
compatibility; the **source command must not use that destructive staging-only mode**.
Existing full/sharded GSD workflows and `remote-verify.sh` are unchanged.

## Fixed contract

| Item | Binding |
|---|---|
| Workflow repository | `pimmink/gsd-pi-ci` |
| Workflow file | `.github/workflows/native-cmux-build-only.yml` |
| Native source repository | `pimmink/cmux` |
| Source branch (must still resolve to pinned SHA) | `fix/gsdpi-vault-resume-cas-20261110` |
| Source HEAD | `33daf63d0152d5c7f26155506f0beb191e5ad3da` |
| Standard hosted runner | **`macos-26-intel`**, x86_64; no dynamic/larger/fleet fallback |
| Source requirements | `.xcode-version`: major 26; macOS 14+ |
| Artifact target host | macOS **15.7.9**, Intel; distinct from runner OS 26 |
| Source build entry point | `CMUX_DEV_BACKEND_MODE=local ./scripts/reload.sh --tag "$BUILD_TAG" --no-global-cli-links --derived-data "$DERIVED_DATA"` |
| Isolation | New `d014-` tag; absent, run-specific derived-data path in runner temp |
| Limits | At most one future dispatch/build, attempt 1 only, no matrix/retry/redispatch; 55-minute execution timeout; native queue must leave queued state before 15 minutes (mandatory external guard; unknown on network failure) |

`--derived-data` is an existing source option; it does not change the build protocol.
Do not change the stock invocation solely to suppress startup. Do not add shared
app names/tags, native source patches, local test/acceptance launches, or global
installation/setup commands. Only the bounded CI tool setup below is
permitted. Missing credentials, incompatible settings, setup failure, or unexpected
global dependencies are diagnostic failures, not permission
to repair the source or weaken the checks. No provider/model calls are part of this route.

### Tracked-source byte proof and exclusions

Prepare, preflight, provider collection and provider verification authenticate the
pinned commit → tree → blob chain without Git replacement refs, then independently
compare **every tracked entry** against the filesystem, recursively through pinned
gitlink commits. Regular-file bytes/executable modes, entry types, symlink target
bytes and initialized submodule HEADs must match. The producer/configuration/lock
allowlists remain additional constraints, not the inventory boundary. Persistent
subtree/ancestor rename fences and before/after physical inventory comparisons
remain held for the complete candidate. Each binding owner shares one event queue
and one descriptor per distinct physical directory path, without dropping any
subtree watch or rearming ancestors. Descriptor demand still grows with the number
of distinct directories; insufficient capacity fails closed. Git-index status,
assume-unchanged and skip-worktree flags are not byte authority; index-only
disagreement is not a tracked-source byte failure.

This is **not** a hermetic-build or generic Git-metadata identity attestation. The
proof excludes Git metadata itself, untracked/ignored files (including generated,
dependency and build inputs), symlink referent contents unless independently
tracked, and Git-unrepresented ownership/ACLs/xattrs/non-executable permission
bits. Existing package/provider bindings and archive checks still apply separately.
No reviewer9759/D025 adjudication, API4229759 completion or GO follows from this fix.

### Runner evidence and readiness (recheck immediately before the single dispatch)

The official [runner-images README at
`f2f91839f985dcbde42542557c72de8d0c2970a6`](https://github.com/actions/runner-images/blob/f2f91839f985dcbde42542557c72de8d0c2970a6/README.md)
lists `macos-26-intel` explicitly as x64. Its [macOS 26 image inventory](https://github.com/actions/runner-images/blob/f2f91839f985dcbde42542557c72de8d0c2970a6/images/macos/macos-26-Readme.md)
lists Xcode 26.6 (17F113) as default. This is label/toolchain evidence, **not live
capacity, approval or budget evidence**. Re-verify current official docs and the
public branch's full SHA before dispatch. Do not guess another label if it queues.

That inventory documents Rust/Cargo 1.98, but does **not document Zig, Bun or
Metal availability**; undocumented is not proven absent. Dependency/build readiness
cannot be inferred from the image label. This candidate prepares the following
minimal setup for the **same one future authorized fresh-CI run**, not locally:

1. `prepare` first verifies GitHub-hosted Darwin/x86_64, attempt 1, reviewed harness
   HEAD, fixed source HEAD/tree and clean recursive submodules; selected Xcode must
   be exactly **26.6 / 17F113**. It rejects existing app/process/launchd/socket/tag
   state, per-run path collisions (including dangling symlinks), inherited CMUX,
   Zig/Bun or shell/loader injection overrides. It retains pinned source proof bytes
   and SHA-256s. Missing/changed proofs fail before setup. At the source proof
   boundary, `prepare FD:` records the verifier Python process's runtime,
   `RLIMIT_NOFILE` soft/hard values, Darwin per-process kernel ceiling, and
   bounded open-descriptor counts before the proof and after its cleanup, even
   when the proof raises. The same summary is in `prepare.json` under
   `prepare_source_fds`. Missing or capped observations are `null`; the count
   delta is not a peak, an admission check, or proof that the proof fits. The
   instrumentation does not change limits or source acceptance.
2. Zig uses only the pinned source's `scripts/install-zig-ci.sh` with
   `ZIG_FORCE_LOCAL_INSTALL=1`, private run-specific `RUNNER_TEMP`/`ZIG_INSTALL_ROOT`,
   and the **exact** `minimum_zig_version` read by its helper from the pinned
   recursive Ghostty manifest. No numeric version is guessed from another checkout.
   The installer checks the official release-index SHA-256; it additionally checks
   minisign when available (absence is not represented as a signature check).
   Its mirrors and at most two attempts per transfer share one 360-second download
   budget. This is bounded tool-download retry, **not retry of a native build**.
   The source's sudo/global path is disabled; only the private compiler directory
   is added to later CI steps' PATH, not `CMUX_ZIG` or a replacement `rm`/shell hook.
3. Bun is pinned to **1.3.6**, matching the fixed source's `ci-macos.yml`, from
   `oven-sh/bun`'s official versioned `bun-darwin-x64.zip` release. The exact archive
   must match its official `SHASUMS256.txt` entry before extracting just the bounded
   regular executable into the private per-run tools directory. Both requests have
   bounded time/size and at most two curl retries. This is same-origin SHA-256
   verification, not an independent release signature. URL/hash/version are retained.
4. If `xcrun --sdk macosx metal --version` fails, make **one** official selected-Xcode
   `xcodebuild -downloadComponent MetalToolchain` call (600-second timeout), then
   require a working compiler. This Apple-managed component path is the sole setup
   exception to private tool directories; no sudo, Xcode switch, Apple ID, login or
   OS permission request is allowed. Failures stop the job; no login/repair loop.

Setup is one-shot and fails if its private tools directory already exists. It never
calls `setup.sh`, Homebrew, a curl-pipe installer, reload, native tests, paid services
or account provisioning. Only Zig/Bun paths are published after successful setup.
Then native preflight still requires all actual tools, exact Zig and Bun versions,
x86_64 Rust host, working Metal, macOS 26 SDK, clean source and deployment targets
<=15.7.9 (the pinned source declares **14.0**, not 15.7.9), with no arm-only override.
Public source-owned resolution inside stock reload remains unchanged. This offline
preparation does not prove downloads, component availability or build compatibility;
unknowns/failures are reported honestly, never repaired by a second build/run.

The **workflow SHA is not the source SHA**. Resolve the reviewed CI ref to a full
40-character SHA immediately before dispatch and supply `expected_workflow_sha`.
The workflow checks it against `github.sha` and the harness checkout. Source HEAD
and tree are recorded separately. A new exclusive tag is also a required input.
Do not rerun jobs to validate this CI implementation: use the offline checks below.

## Queue bound: ONE external parent cancellation authority

`timeout-minutes` bounds execution, **not queueing**. The Linux observer is auxiliary;
its start, overall workflow `in_progress`, or a queued job's populated `started_at`
do not prove that the Mac job started. A positive native start requires the exact
native job name, run/attempt, the explicit Intel label, `in_progress`/`completed`,
a nonzero assigned runner, and a real started step. The first real step's timestamp
is used conservatively for the cap, even if `job.started_at` retains a queued
placeholder after allocation. Missing/ambiguous/future data fail closed.

The queue clock starts at the exact new run's REST `created_at`, not observer/guard
startup. The sole external guard reserves the last minute of the 15-minute cap for bounded API
calls: it begins diagnosis/cancellation at minute 14 if the Mac job still has not
started. It saves run/jobs (including labels, runner assignment, status/times),
configured concurrency and pending-deployment/approval evidence **before** its
single cancellation POST. It revalidates identity and start immediately before
POST. Job-level concurrency has a fixed group and `cancel-in-progress: false`;
this never cancels a previous run automatically.

The guard binds repository, exact run ID, workflow path, workflow HEAD, event,
attempt 1, exact creation timestamp and the source-SHA/tag run title. It refuses
historical/future or late attachments. It cannot dispatch, rerun, force-cancel,
choose another runner, or cancel another repository/run. A terminal run is never
cancelled. A start/cancel race, network failure or HTTP error is preserved as
unknown, **not** a claim that the run was cancelled or that no build started.
An observed native start after the deadline is a bound failure, not a pass.

**The internal observer has `actions: read` and always uses `--observe-only`. It
cannot POST cancellation, including when the external guard is missing or dead.**
It may itself queue or lose its upload when the parent cancels. There is deliberately
no second canceller, takeover, lease, API lock, distributed controller or new service.

The publisher must own exactly **one** foreground external guard, prepared before
any authorized dispatch, and attach it immediately to the one new run (well before
minute 14). Preflight must confirm the reviewed guard file/CI SHA, existing token's
permission for this fixed repo/run, a fresh output directory, accurate UTC clock,
working network, no second external guard, and a responsible parent present until
native start/terminal reconciliation. If any of these cannot be established, **do
not dispatch**. A prepared command is not evidence that a guard is alive:

```bash
# Preconditions: exact new run receipt, reviewed CI_SHA, same EXCLUSIVE_TAG.
# Existing gh authentication only; no new PAT, key, credential or account changes.
set +x
CREATED_AT="$(gh api "repos/pimmink/gsd-pi-ci/actions/runs/$NEW_RUN_ID" --jq .created_at)"
GH_TOKEN="$(gh auth token)" python3 scripts/native_cmux_queue.py \
  --run-id "$NEW_RUN_ID" --workflow-sha "$CI_SHA" \
  --created-at "$CREATED_AT" --tag "$EXCLUSIVE_TAG" \
  --output ".artifacts/d014-native-ci/queue-$NEW_RUN_ID-parent"
```

Identify **exactly one** new run using the workflow, dispatch time, CI HEAD and
source/tag title. If discovery is ambiguous, stop and investigate; never substitute
a remembered run ID. Keep reporting its URL and status while monitoring. Only the
external parent guard may issue its single cancellation POST; the internal observer
never races to POST. Never start a second external guard or retry an unknown POST. API/network outages cannot guarantee server-side cancellation;
the publisher must reconcile the exact run immediately, preserve that uncertainty,
and must not silently proceed or dispatch again. Each watch needs a fresh output
directory (no overwriting prior evidence); at most 100 bounded snapshots are saved.
Every validated poll updates `heartbeat.json` with absolute creation/observation
clocks, identities, role and native state; console progress is emitted roughly every
minute, with the run URL in the terminal result. The parent must notice a stalled
heartbeat, guard exit or API error promptly, preserve the result and STOP for
operator reconciliation. Do not silently detach or auto-restart a cancelling guard.
The minute-14 diagnosis/recheck leaves time for a request **before** minute 15;
it does not guarantee server-side cancellation/completion under network loss,
scheduler delay or a native-start race. Acknowledged POST is not proof of no build.
A start at/after the deadline fails the strict queue condition. An unknown outcome
never clears the cap and never authorizes another build or a historical-run cancel.

If the queue cap is reached without an actual native start: preserve diagnosis,
request cancellation **only for that exact new run**, then **STOP**. The parent
must ask **one** concrete fallback question using `ask_user_questions`, after
reconciling any cancellation/build-start race. The script does not supply an answer,
choose a fallback or authorize a second build. Never cancel historical runs.

## Complete-app materialization contract (F1)

The approved recipe uses the unmodified stock normal tagged mode: **no
`--build-only`, no `--launch`, no launch environment/script injection**. The pinned
`--build-only` finalizer deletes its assembled staging bundle; capturing the base
DerivedData app instead would omit the assembled helpers. No timing interception,
trap cleanup suppression, PATH/rm shim, BASH_ENV hook, native source patch or
acceptance-harness workaround is used.

The supplied `d014-` tag must already equal the pinned helper's lowercase slug:
no repeated/trailing hyphens, no punctuation folding, and at most 64 total ASCII
characters. No custom app name, beta/source change or moving source ref is accepted.
The app is `cmux DEV <slug>.app` at
`<private run root>/derived/Build/Products/Debug/cmux DEV <slug>.app`.
`prepare` derives and exports that exact path once; `materialized` requires the
actual final, non-redirected app and matching name/bundle ID, unchanged HEAD
`33daf63d0152d5c7f26155506f0beb191e5ad3da` and tree
`163b044d6d3b332e6e8a22b303c21960a159058d`, and the retained source-file hashes.
A surviving hidden staging path, missing final app, wrong tag or changed source
fails; there is no fallback to another app or rebuild. Both inspect and package use
that same exported final path. Publication is **not** yet completeness/compatibility
proof: the F2/F3 inventory, helper, dylib-type and archive checks below must pass.

Normal tagged mode can quit/kill a matching tagged app, boot out/remove its launchd
job and publish tag/discovery state, even with `LAUNCH=0`. Hence a **new exclusive
tag and no pre-existing native state** are mandatory, not merely a private DD path.
`--no-global-cli-links` suppresses ambient CLI-link/shim replacement; permitted
stock tag/discovery publication is confined to the disposable runner. It is not a
safe developer-machine recipe. The implementation's source trace is supporting
material only: independent review must cover the lock wrapper, sourced helpers,
normal-mode publication/stop commands (including AppleScript quit semantics),
build phases and all launch call sites for isolation and side effects. Stock CI
startup is permitted by D019; it is not a no-launch gate. Successful offline mocks
still cannot clear independent exact-code review, accounting or execution admission.

## Compatibility and artifact evidence

`native_cmux.py inspect` does not execute the product. It checks:

- Main and embedded executable-bundle Info.plists, declared minimum OS values
  (including per-x86_64 keys), and the presence of their executables.
- Source-defined CLI (`bin/cmux`), Ghostty, cmux-tui, Computer Use, paste worker,
  diff sidecar and explicit Nucleo `dlopen` dylib, even without a main load edge.
  Stock Debug includes the Dock plugin (`MH_BUNDLE`), tunnel system extension
  (`MH_EXECUTE`) and Sparkle framework (`MH_DYLIB`). Whole missing products fail.
  The main executable is resolved from Info.plist (tagged outer name need not match).
  `cmuxd` is **not mandatory**: this exact pin has no tree/gitlink producer and stock
  build/copy steps are conditional. If present, it still gets all host/type checks.
  Every other native image and nested executable bundle is inspected by magic,
  not filename extension or executable bit. Missing/unproven locked-provider
  distribution completeness now **rejects** inspection and packaging; see F2 below.
- An actual x86_64 slice (`lipo -archs`), Mach-O file type (`otool -arch x86_64 -hv`),
  and **x86_64-specific** load commands (`otool -arch x86_64 -l`), with exactly one macOS `LC_BUILD_VERSION` or
  `LC_VERSION_MIN_MACOSX`, minimum OS <=15.7.9. SDK version is not used as minOS.
- Bundle-confined relocatable symlinks/rpaths; non-system dylib load commands must
  resolve to inspected **MH_DYLIB**, never MH_EXECUTE or MH_BUNDLE. Actual
  `LC_LOAD_DYLIB`, `LC_LOAD_WEAK_DYLIB`, `LC_REEXPORT_DYLIB`, `LC_LOAD_UPWARD_DYLIB`
  and `LC_LAZY_LOAD_DYLIB` kinds are retained in reports. Strong/reexport/upward/lazy
  missing dependencies fail. Safe absent **weak** loads have a distinct
  `weak-absent` result with availability/fallback proof **UNPROVEN**, not a runtime
  safety claim. Present invalid weak loads and unsafe weak paths still fail.
  An invalid first existing candidate is not skipped for a later candidate.
  Required non-system dependencies must resolve to bundled Mach-O dylibs. Only Apple `/System/Library` and `/usr/lib` system routes
  (including Swift system rpaths) are allowed, and must have a selected-SDK stub
  declaring an x86_64 macOS target. This is **SDK metadata only**, never proof of
  macOS 15.7.9 APIs, symbols or runtime behaviour. A system rpath alone cannot hide a missing
  framework/library. Developer/build-tree, Homebrew, missing or escaping load
  paths fail. Both flat CLI helpers and nested app/XPC helpers use their own
  MH_EXECUTE anchor, not the outer app's executable directory.
- A before/after full inventory of all files (including non-Mach-O resources),
  directory/file modes and exact symlink targets, plus root path/device/inode bundle
  identity and per-file AND directory device/inode/link count/mtime_ns/ctime_ns.
  F1 additionally fences every resolved ancestor through filesystem root with
  lossless rename/delete/revoke notifications while metadata pathnames are consumed
  and throughout inspection/packaging. Restored directory/root/ancestor ABA rejects,
  even with the original leaf inode/hash/times unchanged. Unrelated child writes in
  global temporary directories or HOME are not rename events and do not reject.
  Unsupported kernels/watch failures/overflow fail closed; no retry or OS changes.
  The existing leaf change identity still catches transient same-inode writes.
  Regular files must have exactly one hardlink; external/equal-byte hardlink aliases
  are not accepted. No external, dangling, absolute or non-relocatable symlinks/special files.
- SHA-256 checksums for every Mach-O and inspected plist, plus archive checksum.
  Packaging requires the exact inspected root identity and full inventory, including
  equal-byte file-to-external-symlink replacements and newly added images. It copies
  into a private temporary tree using anchored directory descriptors and
  `O_NOFOLLOW` for every source component/file (not a following `copytree`), verifies
  that tree against the frozen inspection,
  archives only that copy, and verifies **every actual ZIP member**, type, mode,
  target and file hash against the full inventory. It rechecks original/copy after
  archival. The ZIP stays attempt-private until all fence exit validation passes,
  then is published without overwriting an intervening artifact. Cleanup touches
  only attempt-private paths; public archive paths are never destructively unlinked.
  Diagnostics copy only explicitly allowed bounded files into a fresh private upload
  directory and never admit a ZIP. The fixed pinned source-proof catalog is included
  with copy-stream and retained-file SHA-256 checks; unlisted files remain excluded.
  Every diagnostic action path, including the final bounded seal receipt, shares
  that staging directory so artifact delivery stays flat. Proof bytes count toward
  the combined upload cap. A separate seal step requires successful package
  and diagnostic outcomes plus the verified package receipt, then independently
  hashes the actual retained archive after making it read-only in a private sealed
  directory. Only that successful step exports an app upload path/verified signal.
  The distinct app upload positively requires package success, seal success and the
  verified signal; rejection remains failure even if private cleanup fails. Combined
  app/diagnostic bytes retain the 400 MiB ceiling. Failure logs remain available
  without an app ZIP. This is
  content/copy/archive verification, not OS immutable flags or a compromised-runner
  defence.

Preflight records actual runner image/OS/architecture, Xcode/Swift/Metal/SDK,
Zig/Rust/Bun, project deployment declarations, source HEAD/tree/submodules,
workflow SHA/file checksum, run, tag and results. The artifact includes x64
lipo/load-command evidence, checksums and actual step outcomes. The `.app` is zipped
with `ditto --norsrc` to preserve the portable files/modes/symlink layout; resource
forks/xattrs and uninspected AppleDouble payloads are **not** included/claimed.
A real Apple-tool ZIP round trip is pending, not proven by offline fixtures.
No dSYM/cache/source tree is uploaded.

A successful compile **alone is not compatibility proof**. These static checks
check a constrained declared ABI/loader/minOS contract, **not fully emulated dyld**,
all OS API availability, signing,
permissions, UI, app startup, PTYs, source behavior, or live native acceptance.
Transitive dyld cases outside the inspected image/executable rpath routes fail
closed rather than searching a developer machine. Using the artifact and actual
native acceptance are separate authorized work.

### D019 source binding, loader contexts and limits

The product policy is bound to the exact source SHA/tree above and hashed stock
producer scripts, project, Info.plists, `Package.resolved`, installer and explicit
Nucleo loader source. It is not a caller-controlled required/allow-missing list.
No cmuxd source producer is invented; an unexpected untracked producer is refused.

Each MH_EXECUTE is its own executable anchor, including flat helpers and XPC/system
extensions. The source-defined Dock plugin uses the known Dock executable path,
not cmux's executable or runpaths. Unknown plugin hosts fail closed. Dependency
walks preserve the ordered intermediate-loader LC_RPATH stack under each real
executable context and validate all observed contexts. Unknown target Dock host
runpaths are not guessed; unresolved loads fail. Dylib cycles terminate.

`cmux-tui-ssh` is not a blanket exception. Only the exact four installer target
names, physical mode-0644 files and matching commit/manifest SHA-256 may be remote
SSH DATA. All bytes remain in the identity/hash inventory. A real host load edge
revokes DATA classification and requires the x86_64 slice, host15 minimum and
MH_DYLIB type. The manifest is not independent publisher-attestation proof.

Physical resolution permits contained relative framework `Versions/Current` links
but rejects external-then-internal bounces, absolute/dangling/cyclic links and
escaping rpaths, even for weak dependencies. Existing descriptor/no-external-hardlink
and archive member protections remain mandatory. Limits: 40,000 entries, 2 GiB
bundle bytes, 2,048 Mach-O images, 4 MiB/plist or load-command output, 64 MiB total
Mach-O output, 64 dyld depth, 4,096 contexts, 65,536 edges, 32 symlinks/512 resolution
steps per path. Exceeding any bound fails; no truncation of the validated graph.

### F2: locked-provider collection is mandatory, after the stock build

`native_cmux_providers.py` only reads stock build outputs/caches; it does not resolve,
fetch, download, build or launch anything. It binds the unchanged source pin/tree,
all existing source-producer hashes, exact Package.resolved, project/Debug recipe,
and the source-proven Sentry/Iroh local product edges. Sparkle, Sentry and Iroh
selected binary products require their locked provider Git commit → tree → manifest
blob hash chain, matching physical manifest and manifest URL/checksum. The cached
archive must match that checksum. The unique non-Catalyst macOS x86_64 XCFramework
slice determines the actual required runtime inventory, including every nested
helper/resource; there is no guessed Sparkle helper list or caller allow-missing flag.
Selected static archives are link inputs, not invented runtime requirements. Other
platform slices/development headers are not host runtime payloads. Runtime helper
Mach-O/minOS/dyld checks remain unchanged; remote SSH DATA classification is unchanged.
The exact locked Iroh manifest's local/remote ternary is selected from its real
conditions: presence of `IROH_FORCE_REMOTE_XCFRAMEWORK` (even an empty value), or
absence of all three manifest-declared local binary probes. Existing probe ancestors
remain bound through verification; an active/unproven local input fails closed.
Only ENOENT proves a missing local probe; I/O, permission and non-directory failures
reject unproven selection rather than being treated as absence.
Only that SHA-256-bound manifest conditional and its literal `.byName(name: "Iroh")`
wrapper edge are supported, not a general Swift evaluator. Other conditional,
external or computed dependency forms remain unsupported.
Unknown embedded providers and unsupported/unproven manifest/configuration/layouts
fail closed instead of returning a successful “completeness unknown” receipt.

The reference is not self-authenticating proof: inspection and packaging rederive it
from those actual inputs, compare their SHA-256 and physical identities, and compare
required provider payload content. Only CodeSignOnCopy's signature blob/command and
signature-dependent LINKEDIT sizes are normalized, not executable code. Every
packaged byte still has the full ordinary bundle/ZIP SHA-256 checks. Collector cache
traversal is capped at 40,000 entries/256 archives, 400 MiB per archive and 1,600 MiB
per derivation. No source archive is downloaded as a fallback if stock caches lack it.
`providers-collection.json` records a bounded failure rather than permitting upload.
Actual cache availability/release layouts and genuine signed artifacts remain to be
verified on the sole separately admitted CI run; unit fixtures are not provider proof.

This inspector proves a **static contract only**. Immutable downloaded client
provenance and macOS 15.7.9 API/symbol/runtime/weak-fallback evidence remain obligations.
No actual artifact, host execution or native acceptance is claimed. Code repair is
not independent code approval or publication/dispatch/Mac admission. D022's actual
human ceiling is EUR 3 for ONE new CI run plus short-lived artifacts, with EUR 0.10
conditional planning only. Models are not CI costs; prior spending, reservations,
attempts and queue history are not reset, and no old balance is newly available.
Public standard `macos-26-intel` only/no fallback, at most ONE still-unspent build,
900s new-own queue, 55 minutes, 400 MiB app + 7 MiB queue/1 day, manual dispatch only
and fresh financial/runner/queue admission all still apply. The separate Intel Mac
15.7.9 test app remains technically unadmitted; private GSD is CLOSED_BLOCKED_UNEXECUTED.

## Actual build diagnostics (F5)

The stock build stdout/stderr is piped through the bounded `capture` reader with
`set -euo pipefail`: a failing source build remains a failed step. The pipe is
consumed, not forwarded as an unbounded console dump. At EOF it retains at most
**2 MiB tail** in `build-stdout.log`, with total observed bytes/truncation/checksum.
An `always()` step after actual step-outcome recording collects the real
`/tmp/cmux-reload-${TAG_SLUG}.log` (the validated lowercase exclusive tag has the
same slug) on both success and failure, again retaining at most **2 MiB tail** in
`native-build.log`. Absence and size changes during capture are recorded, not
misreported as complete logs. Collector failures gate the upload rather than
uploading an unchecked/oversized artifact. Hard cancellation/runner loss may
prevent finalizers/upload; the parent must not claim diagnostics were retained then.

`build-logs.json`, `stdout-capture.json`, step outcomes and `artifact-files.json`
record source pin, workflow SHA, run/tag, provenance, sizes, truncation and retained
file checksums. JSON is capped at 8 MiB each; the final manifest has a 32 KiB cap,
with a 64 KiB reserve for manifest/final report. The **final** upload gate includes
both logs, JSON and ZIP in the 400 MiB ceiling. Per-log caps do not enlarge it.
Each artifact upload uses one-day retention. That setting does **not** change
ordinary Actions job-log retention or account/repository log configuration.

No token/env/config dump is collected; build checkout credentials are not persisted,
and the build step receives no Actions token. Recognizable GitHub tokens,
authorization headers and credential query parameters are redacted before retaining
text. Arbitrary secrets emitted by a third-party tool cannot be reliably recognized;
only public source/dependencies and token-free build inputs belong on this route.
Do not enable shell tracing or log credentials to diagnose failures.

**The source-owned inner `/tmp` build log is genuinely unbounded on runner disk**;
this CI capture cap does not bound the source's writes, downloads, disk use or
helper's own logging. No source-helper patch/trap interception/log-deletion shim is
introduced. Captured tail checksums describe retained/redacted text, not the full
original log. Native acceptance and any source-side logging change are separate work.

## Budget gate and storage

Do not infer a new balance from a prior spending cap. The **existing remaining**
authorization is the **original remaining EUR 10**, including earlier spending,
old held slots and reservations; its actual balance is **unknown**. This is not a
new EUR 10 allocation, reset, zero-spend assumption or zero-total-fee claim.
Before dispatch the publisher
must establish those amounts, approval readiness and an explicit conditional
reservation within the remaining cap; if unknown, **dispatch is blocked**.
CI code review/selfchecks may proceed without claiming budget approval. A read-only
2026-11-17 snapshot reported public/enabled CI and zero queued runs at that moment;
that is not continuing capacity or billing approval. Billing API HTTP 404 responses
do **not** prove their cause, lack of fees or available balance. Both jobs
also refuse a private-repository event; only the fixed public-repository route is enabled.

As checked on 2026-11-17, GitHub's [Actions billing documentation](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions)
states standard GitHub-hosted runners in **public repositories** do not consume
included compute usage, and excess shared artifact/Packages storage is **USD
$0.25/GB/month**, accrued over time. Larger runners are billed even for public repos
and are prohibited here. Confirm repository visibility, standard-runner pricing,
account storage usage/allowance, taxes/currency and existing reservations before
reserving; a public runner does **not** imply zero total cost or an available balance.

Both uploads retain artifacts for **1 day**. The app/report upload is capped at
**400 MiB**, with a 2 GiB uncompressed inspection/packaging bound; queue evidence is
bounded to under 7 MiB per watch. At most ~407 MiB is retained in Actions for this
run, not including any pre-existing storage. Retention/accrual/account state determine
actual storage cost; do not book a guessed EUR amount or assume unused allowance.
No paid fleet, AWS, Blacksmith, new service, release channel or secrets are involved.

## Offline implementation checks (no native build)

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_native_cmux.py' -v
ruby -ryaml -e 'YAML.load_file(ARGV[0]); puts "YAML parse: PASS"' .github/workflows/native-cmux-build-only.yml
```

Fixtures exercise fat/x64/arm-only/missing/malformed binaries, old/new minOS,
Info.plists/rpaths/dependencies, exact versus wrong run/repo/time/HEAD/source,
auxiliary-start/native-queued placeholders, deadlines, cancellation races and
unknown outcomes, and no-other-run/no-retry behavior. No network or native tool
is used by the fixtures. Review uncommitted changes independently before commit;
merge/dispatch and any real build remain separate publisher operations.
