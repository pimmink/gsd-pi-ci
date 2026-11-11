<!-- markdownlint-disable MD013 -->

# Owned core-runtime artifacts

This opt-in path builds a **core runtime, not an npm release or a PR/merge gate**.
The ordinary `remote-pr-verification.yml` job still requires branch-head equality
and runs exactly its previous verification steps. `owned_runtime_only=true` skips
that job and selects a separate, bounded `macos-15-intel` job. Default is false.

## Identity and boundaries

| Identity | Meaning |
| --- | --- |
| `expected_sha` | Full lowercase 40-hex source commit in the fixed public `pimmink/gsd-pi` repository; fetched directly and checked out detached |
| `source_ref` | Valid plain source branch; its observed head and observation time are recorded independently, **not required to equal the pinned source commit** on this artifact-only path |
| `github.workflow_sha` | Exact commit of the workflow **and CI-owned packaging/verifying helpers** in `pimmink/gsd-pi-ci`; fetched and asserted separately |
| Run ID / attempt | Distinguishes retries of the same source and workflow pins |
| Actions artifact ID / digest | Outer Actions ZIP identity, recorded in the job summary/API; not the inner tarball checksum |
| `SHA256SUMS` | SHA-256 of the inner tarball, file manifest and provenance; compare to the trusted run's summary |

Permissions remain `contents: read`. There are no new secrets, publishing, deployments
or repository writes. Artifact runs have a unique run-ID/attempt concurrency group and
`cancel-in-progress: false`; they cannot cancel ordinary jobs or other workflows.
Only already-pinned actions from the existing workflow are reused; no new action SHAs.
The dispatch event must already be registered on the repository's default branch;
`--ref` selects this owned implementation branch, not the source branch.

## What the archive contains

The packager stages the source package's `files` allowlist, root/workspace manifests,
compiled SDK/workspace `dist` trees, root and workspace-local `node_modules` (including
hidden `.pnpm` contents), and the explicitly source-built
`native/addon/gsd_engine.darwin-x64.node`. It does **not** rely on `npm pack` bundling
dependencies or the addon. Internal `@gsd`, `@opengsd`, and the four vendored
`@earendil-works` aliases resolve to the candidate's compiled packages, not registry
copies or a global installation. Internal absolute links are made relative; external,
dangling and cyclic links fail closed.

This first implementation intentionally takes the bounded **full installed dependency
closure**, not fragile production pruning. Development/web dependencies can therefore
be present, but web output, browsers and RTK are not built/downloaded as acceptance
requirements. `.bin` wrappers (which can contain runner-specific paths), package-manager
metadata that points to external stores, caches, `.git`, credential directories and
`.env*`/auth configuration are excluded. Do not run pnpm/npm inside the extracted tree.
Nothing links its whole `node_modules` to a global installation.

Limits: **4 GiB regular-file bytes, 250,000 members, 2 GiB compressed archive**;
metadata files are bounded to 64 MiB. Exceeding a cap fails the job rather than uploading
a partial runtime. Retention is explicitly **14 days**, subject to repository policy.

`provenance.json` binds the source SHA/tree/ref observation, workflow SHA/ref/run/attempt,
OS/architecture, actual Node/pnpm/Rust/Python/runner versions, deployment target,
portable flags, native link dependencies, dependency/native input fingerprints,
staged installed-dependency closure fingerprint, addon bytes/hash and complete manifest digest.
The manifest lists path/type/mode/size and file SHA-256 or symlink target for every member.
Rust uses the locked source build and an explicit x86_64 Apple target;
empty `CARGO_ENCODED_RUSTFLAGS` overrides source `target-cpu=native` defaults.
The engine must link only system libraries (plus its own Mach-O library ID).

Node 24.15.0/pnpm 10.12.1 are pinned. Rust stable, runner-image updates and package
lifecycle downloads remain non-reproducible inputs; source/lockfile hashes alone do not
prove installed-byte parity. This is not a deterministic rebuild claim, full-web release,
code-overlay format, globally installed runtime, deployed artifact or live acceptance.

## Parent dispatch (not performed by this implementation task)

Review, commit and push the CI candidate first; publish the separately approved source
repair SHA through its own owner. No sourcefix worktree edits belong to this CI change.
Resolve both full SHAs immediately before dispatch; do not substitute the CI SHA for
the source SHA. The branch names below are this task's exact handoff routes.

```bash
set -euo pipefail
CI=/Users/pimmink/Klanten/gsd-pi-pimmink/worktrees/exec-2916-ci-artifact-20261111
SOURCEFIX=/Users/pimmink/Klanten/gsd-pi-pimmink/worktrees/exec-2916-host-repair-20261111
WORKFLOW_REF=ci/exec-2916-owned-runtime-20261111
SOURCE_REF=fix/2916-host-honesty-20261111
SOURCE_SHA=$(git -C "$SOURCEFIX" rev-parse HEAD)
WORKFLOW_SHA=$(git -C "$CI" rev-parse HEAD)
# Parent must confirm these are the reviewed, already-pushed exact commits.
gh workflow run remote-pr-verification.yml --repo pimmink/gsd-pi-ci \
  --ref "$WORKFLOW_REF" -f source_ref="$SOURCE_REF" \
  -f expected_sha="$SOURCE_SHA" -f owned_runtime_only=true
```

Record the resulting run URL/ID immediately. This lane's success is artifact/native/import
proof only: it is not the ordinary PR test signal. Do not dispatch both lanes while
calling one a substitute for the other. Bound any external watch and preserve the
actual run/attempt and workflow pin, including a retry's new attempt number.

## Parent fetch, verify and unpack (also not performed here)

Set `RUN_ID`/`ATTEMPT` to the actual selected run. Check its workflow/head SHA against
`WORKFLOW_SHA`, its inputs against `SOURCE_SHA`, and its successful artifact job.
For this manual dispatch, Actions `head_sha` describes the **CI repository**, not the
application source. Preserve the API response, artifact ID, outer digest, job summary
and inner tarball checksum as independent evidence.

```bash
set -euo pipefail
PLAN=/Users/pimmink/Klanten/gsd-pi-pimmink/plans/exec-2913-recovery-20261111
OWNED="$PLAN/owned-artifacts/$SOURCE_SHA/$RUN_ID-$ATTEMPT"
ARTIFACT="owned-runtime-$SOURCE_SHA-darwin-x64-$RUN_ID-$ATTEMPT"
mkdir -p "$(dirname "$OWNED")"
mkdir "$OWNED" # exclusive: never reuse an existing run directory
# Verify provenance of this authenticated GitHub download, not just its JSON claims.
gh api "repos/pimmink/gsd-pi-ci/actions/runs/$RUN_ID/attempts/$ATTEMPT" > "$OWNED/actions-run.json"
gh api "repos/pimmink/gsd-pi-ci/actions/runs/$RUN_ID/artifacts?per_page=100" > "$OWNED/actions-artifacts.json"
python3 - "$OWNED" "$WORKFLOW_SHA" "$RUN_ID" "$ATTEMPT" "$ARTIFACT" <<'PY'
import json, sys
from pathlib import Path
owned = Path(sys.argv[1])
workflow_sha, run_id, attempt, name = sys.argv[2:]
run = json.loads((owned / 'actions-run.json').read_text())
assert run['id'] == int(run_id) and run['run_attempt'] == int(attempt)
assert run['head_sha'] == workflow_sha and run['event'] == 'workflow_dispatch'
assert run['conclusion'] == 'success' and run['status'] == 'completed'
artifacts = json.loads((owned / 'actions-artifacts.json').read_text())['artifacts']
selected = [a for a in artifacts if a['name'] == name and not a['expired']]
assert len(selected) == 1, 'Expected exactly one live artifact with this full source/run/attempt name'
print(json.dumps({'artifact_id': selected[0]['id'], 'outer_digest': selected[0]['digest']}))
PY
gh run download "$RUN_ID" --repo pimmink/gsd-pi-ci \
  --name "$ARTIFACT" --dir "$OWNED/bundle"
# Compare SHA256SUMS with the trusted job summary before executing candidate code.
python3 "$CI/scripts/verify-owned-runtime.py" --bundle "$OWNED/bundle" \
  --expected-sha "$SOURCE_SHA" --workflow-sha "$WORKFLOW_SHA" \
  --run-id "$RUN_ID" --attempt "$ATTEMPT" > "$OWNED/archive-verification.json"
python3 "$CI/scripts/verify-owned-runtime.py" --bundle "$OWNED/bundle" \
  --expected-sha "$SOURCE_SHA" --workflow-sha "$WORKFLOW_SHA" \
  --run-id "$RUN_ID" --attempt "$ATTEMPT" --destination "$OWNED/unpacked" \
  --probe-node /usr/local/bin/node > "$OWNED/host-import-verification.json"
```

Use the reviewed CI helpers at the exact recorded `WORKFLOW_SHA` for these commands.
Checksums/provenance are integrity records, **not signatures**: do not trust a bundle
from an unauthenticated origin merely because all its self-reported fields match.
If cryptographically checking the outer Actions digest, retain the original Actions ZIP
and hash it; hashing the inner tarball cannot validate the outer ZIP digest.
`verification.json` in the bundle is the CI probe report, not a separately authenticated
certificate. Always produce the fresh local verifier receipt above.

The verifier parses a small, bounded GNU tar subset and checks every member before
creating the destination. It rejects absolute/traversal/control-character paths,
case collisions, duplicate members, hardlinks, devices/FIFOs, sparse/PAX headers,
special modes, excessive expansion, missing parents, escaping/dangling/cyclic links
(including symlink-plus-`..` escapes), and any content/manifest mismatch. It never uses
`extractall`, overlays an existing directory, or follows links while writing files.

`--probe-node` explicitly executes reviewed candidate imports, **not the CLI host**.
It uses a fresh allowlisted environment with all writable state under `probe-state`,
no inherited `NODE_PATH`/`NODE_OPTIONS`, guards CJS/ESM resolution and native loads
against paths outside the extracted tree, resolves internal/required dependencies,
actually loads and hashes the source-native addon through its wrapper, requires both
identity-lock exports, rejects native fallback, and imports three public SDK entrypoints.
A post-probe inventory must still match. Resolution evidence is labelled resolution,
not execution coverage; optional dynamic feature paths remain unproven.

## Bounded owned launch commands for the parent

The parent can use the existing `harness/owned_process.py` and process-scoped observer
with the **absolute extracted bootstrap**, never `/usr/local/bin/gsd` or a global
package path. The old parent launch scripts contain global paths; do not run them
unchanged or silently repoint global links. This identity smoke scaffold is not the
presentation/transport acceptance driver, and starts no other window.

After the preceding verification, run this once per selected mode (`native` or `mcp`)
in a new owned directory. It uses only the existing credential-free fixture provider.
For native visual acceptance, the parent must attach its separately owned terminal
and replay/visual driver to this same absolute candidate; a headless PTY is not a
visual PASS. Do not restart the currently running GSD or install anything globally.

```bash
python3 - "$PLAN" "$OWNED" "$SOURCE_SHA" mcp <<'PY'
import json, os, signal, sys, time
from pathlib import Path
plan, owned = (Path(s).resolve() for s in sys.argv[1:3])
source_sha, mode = sys.argv[3:5]
assert mode in ('native', 'mcp')
sys.path.insert(0, str(plan / 'harness'))
from owned_process import OwnedProcess
root = owned / ('launch-' + mode)
root.mkdir(mode=0o700) # exclusive; choose a fresh name for a rerun
for name in ('workspace', 'home', 'tmp', 'coverage', 'observer'):
    (root / name).mkdir()
agent = root / 'gsd-home/agent'
agent.mkdir(parents=True)
(agent / 'settings.json').write_text(json.dumps({
    'theme': 'dark', 'defaultProvider': 'exec-2913-local', 'defaultModel': 'exec-2913-fixture'}))
runtime = owned / 'unpacked/runtime'
env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
       'HOME': str(root / 'home'), 'GSD_HOME': str(root / 'gsd-home'),
       'GSD_CODING_AGENT_DIR': str(agent), 'TMPDIR': str(root / 'tmp'),
       'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
       'NODE_COMPILE_CACHE': str(root / 'node-cache'), 'NODE_V8_COVERAGE': str(root / 'coverage'),
       'EXEC2913_OBSERVER_DIR': str(root / 'observer'), 'EXEC2913_CANDIDATE_ROOT': str(runtime),
       'EXEC2913_EXPECTED_SOURCE_SHA': source_sha, 'EXEC_2913_OUTPUT_DIR': str(root),
       'TERM': 'xterm-256color', 'GSD_NATIVE_PREFER_LOCAL': '1', 'GSD_RTK_DISABLED': '1',
       'GSD_SKIP_DEP_REPAIR': '1', 'GSD_SKIP_RTK_INSTALL': '1'}
argv = ['/usr/local/bin/node', '--require', str(plan / 'runtime-observer.cjs'),
        str(runtime / 'dist/bootstrap.js'), '--no-session', '--extension',
        str(plan / 'fixtures/extension.mjs'), '--model', 'exec-2913-local/exec-2913-fixture']
if mode == 'mcp':
    argv += ['--mode', 'mcp'] # embedded host, NOT packages/mcp-server/bin/*
with OwnedProcess(argv, artifacts=root / 'process', timeout=30, cwd=root / 'workspace',
                  env=env, use_pty=(mode == 'native'), max_output_bytes=4*1024*1024) as child:
    if mode == 'mcp':
        child.send_json({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'owned-runtime-identity', 'version': '1'}}})
        response = child.read_json()
        assert response.get('id') == 1 and 'result' in response, 'MCP initialization failed'
        child.send_json({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    else:
        until = time.monotonic() + 15
        while time.monotonic() < until:
            child.read_available(timeout=.05)
    os.kill(child.pid, signal.SIGUSR2) # fresh owned leader, no PID adoption
    time.sleep(.25)
    child.close(reason='identity-smoke-only')
assert child.result.leader_reaped and not child.result.cleanup_errors
print(json.dumps({'scope': 'identity smoke only', 'receipt': str(root / 'process/result.json')}))
PY
```

Retain source/workflow/inner/outer identities with supervisor receipts, observer native
paths/hashes and V8 coverage of the relevant compiled modules. Successful packaging,
import probing and even fresh process launch are distinct states. Only the parent's
actual source-bound native/MCP presentation checks can establish the final host verdict.

## Local focused verification

No local full build, install or host start is needed:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -v
node --check scripts/probe-owned-runtime.mjs
actionlint .github/workflows/remote-pr-verification.yml
git diff --check
```

The security tests use tiny synthetic trees/binaries, not sourcefix build output.
Online native/dependency/runner behavior remains unverified until the parent dispatches
this exact CI commit with the approved exact source SHA and examines its artifacts.
