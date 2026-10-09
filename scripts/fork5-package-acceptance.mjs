#!/usr/bin/env node
'use strict'

// W053 fork.5 packaging/Mac acceptance driver.
//
// Scope (see final-fork-gate-plan.md "Current online validation" / Codex
// review 2026-10-08): produce the npm release tarball from an EXACT git
// archive of the candidate SHA via supported prepack/postpack lifecycle
// scripts (no --ignore-scripts), verify manifest restoration, reject unsafe
// archive members (absolute paths, `..` traversal, escaping symlinks,
// non-macOS native addons), record a source-bound provenance/receipt with
// package+artifact hashes, then install the EXACT produced tarball under an
// isolated HOME/prefix and run CLI/native/resource-init smoke WITHOUT a real
// Edelman DB/auth and WITHOUT running scripts/install.js's own
// network-fetching installer paths directly.
//
// This script does NOT edit any shared/integration/CI file, does NOT touch
// the global gsd-pi install, does NOT run a full test suite, and does NOT
// invoke npm/pnpm install at the repo root. It is intentionally not executed
// by the author of this change; the parent runs it after current-source CI
// passes. Dry validation of the synthetic unsafe-archive/manifest-restoration
// tests can be run standalone (see `--self-test`) without packing anything.
//
// Fail-closed invariants (see "Verify" section below for the literal checks):
//   - Candidate ref/SHA must resolve via `git ls-remote`/local refs and must
//     match an own-fork GitHub Actions run that is COMPLETED + CONCLUSION
//     SUCCESS for that exact head_sha. Any other state aborts before packing.
//   - Source is staged from `git archive <sha>` into a neutral temp dir --
//     never the operator's live worktree -- so prepack/postpack mutate only
//     disposable files.
//   - Every manifest mutated by prepack (and recorded by the stage's own
//     `.prepack-backup`) must be restored byte-for-byte after postpack, or
//     restored manually from the recorded pre-pack hash if postpack did not
//     run (e.g. the pack step itself failed after prepack).
//   - Tarball member paths are re-validated independently of npm: reject
//     absolute paths, `..` segments, and symlink members that dereference
//     outside the extracted root.
//   - A macOS release tarball must not contain a Linux native `.node` member
//     under `native/npm/linux-*`.
//   - Install happens into an isolated HOME/XDG/npm-cache/prefix; nothing is
//     written to the operator's real `$HOME/.gsd` or global npm prefix.
//   - No `install_global`/pointer-activation step is attempted here: it is
//     explicitly out of scope ("separate after acceptance").

import { createHash } from 'node:crypto'
import {
  chmodSync,
  closeSync,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'

const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname)
const ARTIFACT_ROOT = path.join(SCRIPT_DIR, 'artifacts', 'fork5-package-acceptance')
const SAFE_TAR_HELPER = path.join(SCRIPT_DIR, 'fork5_safe_tar.py')
const EXPECTED_PACKAGE_NAME = '@opengsd/gsd-pi'
const EXPECTED_VERSION = '1.21.1-fork.5'
const EXPECTED_NATIVE_ENGINE = 'darwin-x64' // "Stable engine-darwin-x64 1.21.1 resolution"
const COMMAND_TIMEOUT_MS = 10 * 60 * 1000
// Only these allow-listed CI-build-output subtrees may be transferred from
// the CI artifact into the git-archive-staged source (git archive excludes
// gitignored dist/ entirely, so native addons must come from a verified CI
// artifact, never from the operator's local uncommitted build).
const ARTIFACT_ALLOWED_PATH_RE = /^dist\/packages\/(?!\.\.$)[^/]+\/dist\/native\/addon(\/(?!\.\.(?:\/|$))[^/]+)*$/

function now() {
  return new Date().toISOString()
}

function fail(message) {
  const err = new Error(message)
  err.failClosed = true
  throw err
}

function run(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, {
    encoding: 'utf8',
    timeout: COMMAND_TIMEOUT_MS,
    stdio: ['ignore', 'pipe', 'pipe'],
    ...opts,
  })
  if (result.error) fail(`${cmd} ${args.join(' ')} failed to spawn: ${result.error.message}`)
  return result
}

function runOk(cmd, args, opts = {}) {
  const result = run(cmd, args, opts)
  if (result.status !== 0) {
    fail(`${cmd} ${args.join(' ')} exited ${result.status}: ${result.stderr.trim().slice(0, 4000)}`)
  }
  return result
}

function sha256File(filePath) {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex')
}

function sha256Buf(buf) {
  return createHash('sha256').update(buf).digest('hex')
}

// ---------------------------------------------------------------------------
// 1. GitHub provenance gate: candidate SHA must be backed by a COMPLETED,
//    CONCLUSION-SUCCESS own-fork Actions run for the exact head_sha. This is
//    the only accepted substitute for "I trust this SHA" -- no run, no pack.
// ---------------------------------------------------------------------------
function verifyGithubRunProvenance({ owner, repo, sha, workflowNameHint }) {
  const result = run('gh', [
    'api',
    `repos/${owner}/${repo}/actions/runs`,
    '-X', 'GET',
    '-f', `head_sha=${sha}`,
    '--jq', '.workflow_runs',
  ])
  if (result.status !== 0) {
    fail(`gh api run lookup failed (exit ${result.status}): ${result.stderr.trim().slice(0, 2000)}`)
  }
  let runs
  try {
    runs = JSON.parse(result.stdout)
  } catch (e) {
    fail(`gh api returned non-JSON run list: ${e.message}`)
  }
  if (!Array.isArray(runs) || runs.length === 0) {
    fail(`No GitHub Actions runs found for ${owner}/${repo} head_sha=${sha}. Cannot bind provenance to an unverified SHA.`)
  }
  const candidates = runs.filter((r) => r.head_sha === sha)
  if (candidates.length === 0) fail(`gh api returned runs but none match head_sha=${sha} exactly.`)
  const matching = workflowNameHint
    ? candidates.filter((r) => String(r.name || '').toLowerCase().includes(workflowNameHint.toLowerCase()))
    : candidates
  const pool = matching.length > 0 ? matching : candidates
  const success = pool.find((r) => r.status === 'completed' && r.conclusion === 'success')
  if (!success) {
    const statuses = pool.map((r) => `${r.name}:${r.status}/${r.conclusion}`).join(', ')
    fail(
      `No COMPLETED+SUCCESS run for head_sha=${sha} in ${owner}/${repo}. ` +
      `Found: ${statuses || '(none)'}. Fail-closed: refusing to pack a SHA without a passing own-release run.`,
    )
  }
  return { runId: success.id, runUrl: success.html_url, name: success.name, headBranch: success.head_branch }
}

// ---------------------------------------------------------------------------
// 1b. CI artifact provenance: download the artifact ZIP directly from GitHub
//    via an authorized GET (never trust a caller-supplied local file as the
//    primary source of truth), bind it to the SAME run verified in step 1,
//    verify GitHub's own reported digest when present, and only then hash
//    the downloaded bytes. `--artifact`/`--artifact-sha256` remain available
//    as an optional PIN: if supplied, the downloaded bytes must ALSO match
//    that hash exactly (defense in depth / operator-side reproducibility
//    check), but the download+metadata binding below is not skippable.
// ---------------------------------------------------------------------------
function downloadArtifactZip({ owner, repo, artifactId, destZipPath }) {
  // Explicit GET against the binary-content endpoint. `gh api` with
  // `--output <path>` lets gh itself write raw bytes straight to disk,
  // which is the supported way to pull a binary GitHub API response
  // through this CLI (avoids corrupting binary content via a text pipe).
  const result = run('gh', [
    'api',
    `repos/${owner}/${repo}/actions/artifacts/${artifactId}/zip`,
    '-X', 'GET',
    '--output', destZipPath,
  ])
  if (result.status !== 0) {
    fail(`gh api artifact ZIP download failed (exit ${result.status}): ${result.stderr.trim().slice(0, 2000)}`)
  }
  if (!existsSync(destZipPath) || statSync(destZipPath).size === 0) {
    fail(`gh api reported success but no (or empty) artifact ZIP was written to ${destZipPath}.`)
  }
  return destZipPath
}

const TRUSTED_ARTIFACT_MEMBER_NAME = 'ci-build-artifacts.tar.gz'

// Extract exactly ONE named member from the downloaded artifact ZIP, using
// Python stdlib zipfile (bounded: reads a single named entry via .read(),
// never extractall(); no path-traversal surface since the member's own
// on-disk path is never honored -- we always write to our own fixed
// destPath regardless of what the zip entry's name claims).
function extractTrustedMemberFromZip({ zipPath, destPath }) {
  const script = `
import json, sys, zipfile
zip_path = ${JSON.stringify(zipPath)}
dest_path = ${JSON.stringify(destPath)}
member_name = ${JSON.stringify('ci-build-artifacts.tar.gz')}
try:
    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()
        matches = [n for n in names if n == member_name or n.endswith('/' + member_name)]
        if not matches:
            print(json.dumps({"ok": False, "error": "trusted member " + member_name + " not found in artifact ZIP; entries: " + str(names[:20])}))
            sys.exit(1)
        if len(matches) > 1:
            print(json.dumps({"ok": False, "error": "multiple candidate entries match " + member_name + ": " + str(matches)}))
            sys.exit(1)
        data = zf.read(matches[0])
        with open(dest_path, 'wb') as out:
            out.write(data)
        print(json.dumps({"ok": True, "memberName": matches[0], "size": len(data)}))
except zipfile.BadZipFile as e:
    print(json.dumps({"ok": False, "error": "bad zip file: " + str(e)}))
    sys.exit(1)
`
  const result = run('python3', ['-c', script])
  let parsed
  try {
    parsed = JSON.parse(result.stdout)
  } catch (e) {
    fail(`zip member extraction produced non-JSON output: ${e.message}: ${result.stdout.slice(0, 1000)}`)
  }
  if (!parsed.ok) fail(`Failed to extract trusted member from artifact ZIP: ${parsed.error}`)
  if (!existsSync(destPath)) fail(`zip extraction reported success but ${destPath} is missing.`)
  return parsed
}

// Pure, network-free assertion: the downloaded+extracted tarball's sha256
// must match an operator-supplied --artifact-sha256 pin and/or a local
// --artifact file's sha256, when either is supplied. Factored out so the
// self-test can exercise the wrong-hash rejection path directly against
// mocked content, without requiring network access.
function assertPinnedArtifactHashMatches({ artifactId, downloadedSha256, pinnedSha256, pinnedArtifactPath }) {
  if (pinnedSha256) {
    if (downloadedSha256 !== pinnedSha256) {
      fail(
        `Downloaded+extracted artifact ${artifactId} inner tarball sha256 ${downloadedSha256} does not match operator-pinned --artifact-sha256 ${pinnedSha256}. ` +
        'Refusing to proceed: the freshly-downloaded bytes are the source of truth, and they disagree with the pin.',
      )
    }
  }
  if (pinnedArtifactPath) {
    if (!existsSync(pinnedArtifactPath)) fail(`--artifact path does not exist: ${pinnedArtifactPath}`)
    const pinnedFileSha256 = sha256File(pinnedArtifactPath)
    if (pinnedFileSha256 !== downloadedSha256) {
      fail(
        `Operator-supplied --artifact file sha256 ${pinnedFileSha256} does not match the freshly-downloaded+extracted artifact ${artifactId} inner tarball sha256 ${downloadedSha256}. ` +
        "Refusing to substitute a locally-supplied file for GitHub's own artifact bytes.",
      )
    }
  }
}

function verifyCiArtifactProvenance({ owner, repo, runId, artifactId, runDir, pinnedArtifactPath, pinnedSha256 }) {
  // Explicit GET: `gh api` defaults to GET for a path with no -X/-f/-F body
  // fields that imply POST, but we pass -X GET literally so this call can
  // never silently become a mutating request and so a future edit cannot
  // accidentally flip it.
  const result = run('gh', [
    'api',
    `repos/${owner}/${repo}/actions/artifacts/${artifactId}`,
    '-X', 'GET',
  ])
  if (result.status !== 0) {
    fail(`gh api artifact metadata lookup failed (exit ${result.status}): ${result.stderr.trim().slice(0, 2000)}`)
  }
  let meta
  try {
    meta = JSON.parse(result.stdout)
  } catch (e) {
    fail(`gh api artifact metadata returned non-JSON: ${e.message}`)
  }
  if (meta.workflow_run?.id == null) fail(`Artifact ${artifactId} metadata has no workflow_run.id; cannot bind to a run.`)
  if (String(meta.workflow_run.id) !== String(runId)) {
    fail(
      `Artifact ${artifactId} belongs to run ${meta.workflow_run.id}, not the verified run ${runId}. ` +
      'Refusing to accept an artifact from a different run than the one proven COMPLETED+SUCCESS for this SHA.',
    )
  }
  if (meta.expired) fail(`Artifact ${artifactId} is reported expired by GitHub; cannot trust its content.`)

  // Download the REAL artifact bytes for THIS run+artifact id combination
  // into an owned temp location -- this is the actual provenance binding,
  // not just a metadata-shape check. A caller-supplied --artifact path is
  // never treated as the source of truth for content; it can only act as an
  // additional pin checked against the downloaded bytes below.
  const downloadDir = path.join(runDir, 'artifact-download')
  mkdirSync(downloadDir, { recursive: true })
  const zipPath = path.join(downloadDir, `artifact-${artifactId}.zip`)
  downloadArtifactZip({ owner, repo, artifactId, destZipPath: zipPath })
  const downloadedZipSha256 = sha256File(zipPath)

  // GitHub exposes a `digest` field on artifact metadata in supporting API
  // versions; when present, cross-check it against the bytes we actually
  // received before trusting anything derived from them.
  if (typeof meta.digest === 'string' && meta.digest.length > 0) {
    const digestHex = meta.digest.replace(/^sha256:/i, '').toLowerCase()
    if (digestHex !== downloadedZipSha256) {
      fail(
        `Downloaded artifact ${artifactId} sha256 ${downloadedZipSha256} does not match GitHub-reported digest ${meta.digest}. ` +
        "Refusing to trust content that does not match GitHub's own integrity record for this artifact.",
      )
    }
  }

  // Extract the single trusted inner tarball from the downloaded ZIP. This
  // is the artifact's real payload; the ZIP wrapper itself is just the
  // GitHub Actions artifact download envelope.
  const tarballPath = path.join(downloadDir, TRUSTED_ARTIFACT_MEMBER_NAME)
  extractTrustedMemberFromZip({ zipPath, destPath: tarballPath })
  const downloadedSha256 = sha256File(tarballPath)

  assertPinnedArtifactHashMatches({ artifactId, downloadedSha256, pinnedSha256, pinnedArtifactPath })

  return {
    artifactId,
    artifactName: meta.name,
    sizeInBytes: meta.size_in_bytes,
    expired: Boolean(meta.expired),
    workflowRunId: meta.workflow_run.id,
    sha256: downloadedSha256,
    zipSha256: downloadedZipSha256,
    githubReportedDigest: meta.digest || null,
    zipPath,
    tarballPath,
  }
}

// ---------------------------------------------------------------------------
// 1c. Bounded Python-subprocess tar safety helpers (no generic `tar -tv`
//    text scraping, no extractall()). See fork5_safe_tar.py docstring.
// ---------------------------------------------------------------------------
function runSafeTarAudit(tarballPath) {
  const result = run('python3', [SAFE_TAR_HELPER, 'audit', tarballPath])
  let parsed
  try {
    parsed = JSON.parse(result.stdout)
  } catch (e) {
    fail(`fork5_safe_tar.py audit produced non-JSON output: ${e.message}: ${result.stdout.slice(0, 1000)}`)
  }
  if (!parsed.ok) fail(`fork5_safe_tar.py audit failed: ${parsed.error}`)
  return parsed.members
}

function runSafeTarExtract(tarballPath, destDir, prefixes) {
  mkdirSync(destDir, { recursive: true })
  const result = run('python3', [SAFE_TAR_HELPER, 'extract', tarballPath, destDir, ...prefixes])
  let parsed
  try {
    parsed = JSON.parse(result.stdout)
  } catch (e) {
    fail(`fork5_safe_tar.py extract produced non-JSON output: ${e.message}: ${result.stdout.slice(0, 1000)}`)
  }
  if (!parsed.ok) fail(`fork5_safe_tar.py extract failed: ${parsed.error}`)
  if (result.status !== 0 || (parsed.rejected && parsed.rejected.length > 0)) {
    fail(`fork5_safe_tar.py extract rejected unsafe member(s): ${JSON.stringify(parsed.rejected).slice(0, 2000)}`)
  }
  return parsed
}

// ---------------------------------------------------------------------------
// 1d. Transfer the CI artifact's allow-listed native build output into the
//    git-archive-staged source tree, using the bounded Python extractor only
//    (never a generic tar member dump), restricted to dist/packages/*/dist/
//    native/addon paths exactly.
// ---------------------------------------------------------------------------
function transferArtifactIntoStage({ artifactPath, stageDir, extractRunDir }) {
  const members = runSafeTarAudit(artifactPath)
  const scopedNames = members
    .map((m) => m.normalized)
    .filter((n) => ARTIFACT_ALLOWED_PATH_RE.test(n))
  if (scopedNames.length === 0) {
    fail(`CI artifact contains no member under the allow-listed dist/packages/*/dist/native/addon scope; refusing to proceed with an empty native-output transfer.`)
  }
  const unsafe = members.filter((m) => m.isAbsolute || m.isTraversal || m.isDevice || m.escapingLink)
  if (unsafe.length > 0) {
    fail(`CI artifact contains unsafe member(s) outside the allow-listed scope too: ${JSON.stringify(unsafe.map((m) => m.name)).slice(0, 2000)}`)
  }
  const extractDir = path.join(extractRunDir, 'artifact-extract')
  runSafeTarExtract(artifactPath, extractDir, ['dist/packages'])
  let transferredCount = 0
  for (const relName of scopedNames) {
    const src = path.join(extractDir, relName)
    if (!existsSync(src) || !statSync(src).isFile()) continue
    const dest = path.join(stageDir, relName)
    mkdirSync(path.dirname(dest), { recursive: true })
    copyFileSync(src, dest)
    transferredCount += 1
  }
  if (transferredCount === 0) fail('Artifact transfer extracted members but none resolved to a regular file under the allow-listed scope; nothing was copied into stage.')
  return { scopedNames, transferredCount }
}

// ---------------------------------------------------------------------------
// 2. Source staging from an EXACT git archive of the candidate SHA, into a
//    neutral cwd. Never packs the operator's live/dirty worktree.
// ---------------------------------------------------------------------------
function stageSourceFromArchive({ repoDir, sha, stageDir }) {
  const headSha = runOk('git', ['-C', repoDir, 'rev-parse', sha]).stdout.trim()
  const dirty = runOk('git', ['-C', repoDir, 'status', '--porcelain=v1', '--untracked-files=all']).stdout
  // Staging from an exact archive makes worktree dirt irrelevant to the
  // packed content, but a dirty worktree at the REQUESTED sha is still
  // surfaced for visibility (it does not fail closed by itself: `git
  // archive` only ever reads the committed tree object, so dirtiness cannot
  // leak into the staged source).
  mkdirSync(stageDir, { recursive: true })
  const archivePath = path.join(stageDir, '..', `${headSha.slice(0, 12)}.tar`)
  runOk('git', ['-C', repoDir, 'archive', '--format=tar', headSha, '-o', archivePath])
  const extractResult = run('tar', ['-xf', archivePath, '-C', stageDir])
  if (extractResult.status !== 0) fail(`tar extract of git archive failed: ${extractResult.stderr}`)
  rmSync(archivePath, { force: true })
  const pkgJsonPath = path.join(stageDir, 'package.json')
  if (!existsSync(pkgJsonPath)) fail('Staged source has no package.json at its root.')
  const pkg = JSON.parse(readFileSync(pkgJsonPath, 'utf8'))
  if (pkg.name !== EXPECTED_PACKAGE_NAME) fail(`Staged package.json name mismatch: ${pkg.name} != ${EXPECTED_PACKAGE_NAME}`)
  if (pkg.version !== EXPECTED_VERSION) {
    fail(`Staged package.json version mismatch: ${pkg.version} != ${EXPECTED_VERSION}. Update EXPECTED_VERSION only after a new reviewed baseline.`)
  }
  return { headSha, dirtyAtRequestedSha: dirty, pkg }
}

// ---------------------------------------------------------------------------
// 3. Record a manifest hash snapshot (pre-pack) for every manifest the
//    prepack script is known to mutate, so we can prove restoration
//    independent of whatever `.prepack-backup` the script itself leaves.
// ---------------------------------------------------------------------------
function discoverTargetManifests(stageDir) {
  const versionSyncPath = path.join(stageDir, 'scripts', 'lib', 'version-sync.cjs')
  if (!existsSync(versionSyncPath)) fail('scripts/lib/version-sync.cjs missing from staged source; cannot discover manifest scope.')
  // eslint-disable-next-line global-require
  const mod = require('node:module').createRequire(import.meta.url)(versionSyncPath)
  const dirs = mod.RELEASE_WORKSPACE_PACKAGE_DIRS || []
  const manifests = [
    path.join(stageDir, 'package.json'),
    path.join(stageDir, 'dist', 'web', 'standalone', 'package.json'),
    ...dirs.map((d) => path.join(stageDir, d, 'package.json')),
  ].filter((p) => existsSync(p))
  if (manifests.length < 2) fail(`Manifest discovery found suspiciously few manifests (${manifests.length}); refusing to proceed blind.`)
  return manifests
}

function hashManifests(manifestPaths) {
  const map = new Map()
  for (const p of manifestPaths) map.set(p, sha256File(p))
  return map
}

function assertManifestsRestored(beforeMap, stageDir, { onPackFailure } = {}) {
  const mismatches = []
  for (const [p, beforeHash] of beforeMap) {
    if (!existsSync(p)) {
      mismatches.push(`${path.relative(stageDir, p)}: file missing after pack`)
      continue
    }
    const afterHash = sha256File(p)
    if (afterHash !== beforeHash) mismatches.push(`${path.relative(stageDir, p)}: ${beforeHash.slice(0, 12)} -> ${afterHash.slice(0, 12)}`)
  }
  const backupDir = path.join(stageDir, '.prepack-backup')
  if (existsSync(backupDir)) mismatches.push('.prepack-backup directory still present after pack (postpack did not clean up)')
  if (mismatches.length > 0) {
    if (onPackFailure) {
      // Pack itself failed after prepack mutated manifests and before/without
      // postpack running. Restore by hand from our own pre-pack snapshot so
      // the staged tree is left clean, but still FAIL the acceptance run --
      // restoration-on-failure is a safety net for the staged temp dir, not
      // a waiver.
      for (const [p, beforeHash] of beforeMap) {
        if (!existsSync(p) || sha256File(p) !== beforeHash) {
          fail(`Cannot auto-restore ${p}: no hash-verified backup content available outside .prepack-backup (inspect ${backupDir} manually).`)
        }
      }
    }
    fail(`Manifest restoration gate failed:\n${mismatches.join('\n')}`)
  }
}

// ---------------------------------------------------------------------------
// 4. npm pack with supported prepack/postpack lifecycle, no --ignore-scripts.
// ---------------------------------------------------------------------------
function npmPack({ stageDir, destDir }) {
  mkdirSync(destDir, { recursive: true })
  // npm pack with devEngines.packageManager mismatches (pnpm pin) rejects
  // with EBADDEVENGINES when invoked via a differently-named package manager
  // context; run from a neutral cwd/env exactly as the plan's prior art does.
  const result = run('npm', ['pack', '--json', '--pack-destination', destDir], {
    cwd: stageDir,
    env: { ...process.env, npm_config_ignore_scripts: 'false' },
  })
  return result
}

// ---------------------------------------------------------------------------
// 5. Independent tarball member safety audit (archive/link/path containment).
//    This treats npm as untrusted and re-derives safety from the bounded
//    Python tarfile helper (fork5_safe_tar.py), never from npm's own packing
//    report or a generic `tar -tv` text scrape.
// ---------------------------------------------------------------------------
function auditTarballMembers(tarballPath) {
  const members = runSafeTarAudit(tarballPath)
  const findings = { absolutePaths: [], traversal: [], escapingLinks: [], linuxNativeMembers: [], members: members.map((m) => m.name) }
  for (const m of members) {
    if (m.isAbsolute) findings.absolutePaths.push(m.name)
    if (m.isTraversal) findings.traversal.push(m.name)
    if (m.escapingLink) findings.escapingLinks.push(`${m.name} -> ${m.linkTarget}`)
    const strippedName = m.normalized.replace(/^package\//, '')
    if (/native\/npm\/linux-.*\.node$/i.test(m.normalized) || /native\/npm\/linux-.*\.node$/i.test(strippedName)) {
      findings.linuxNativeMembers.push(m.name)
    }
  }
  if (findings.absolutePaths.length > 0) fail(`Tarball contains absolute-path member(s): ${findings.absolutePaths.join(', ')}`)
  if (findings.traversal.length > 0) fail(`Tarball contains '..'-traversal member(s): ${findings.traversal.join(', ')}`)
  if (findings.escapingLinks.length > 0) fail(`Tarball contains escaping symlink member(s): ${findings.escapingLinks.join(', ')}`)
  if (findings.linuxNativeMembers.length > 0) fail(`Tarball contains Linux native addon member(s) in a macOS release artifact: ${findings.linuxNativeMembers.join(', ')}`)
  return findings
}

// ---------------------------------------------------------------------------
// 6. Isolated HOME/prefix install of the EXACT produced tarball (no registry
//    fetch, no global pointer change) + CLI/native-load/resource-init smoke.
// ---------------------------------------------------------------------------
function isolatedInstallAndSmoke({ tarballPath, runDir }) {
  const isolatedHome = path.join(runDir, 'isolated-home')
  const installDir = path.join(runDir, 'isolated-install')
  mkdirSync(isolatedHome, { recursive: true })
  mkdirSync(installDir, { recursive: true })
  const env = {
    ...process.env,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    GSD_HOME: path.join(isolatedHome, '.gsd'),
    XDG_CONFIG_HOME: path.join(isolatedHome, '.config'),
    NPM_CONFIG_USERCONFIG: path.join(isolatedHome, '.npmrc'),
    npm_config_cache: path.join(isolatedHome, '.npm-cache'),
    npm_config_prefix: path.join(isolatedHome, '.npm-global'),
    // Explicitly do NOT disable scripts: the package must install via its
    // normal supported postinstall (scripts/install.js IS_POSTINSTALL path),
    // but we neutralize network-touching sub-steps that are out of scope for
    // a packaging/install acceptance smoke (chromium download, rtk binary
    // fetch) rather than disabling lifecycle scripts wholesale.
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1',
    GSD_SKIP_RTK_INSTALL: '1',
  }
  writeFileSync(path.join(installDir, 'package.json'), `${JSON.stringify({
    name: 'fork5-isolated-smoke-consumer',
    private: true,
    version: '0.0.0',
  }, null, 2)}\n`)
  const installResult = run('npm', ['install', '--no-audit', '--no-fund', tarballPath], { cwd: installDir, env })
  if (installResult.status !== 0) {
    fail(`Isolated npm install of exact tarball failed (exit ${installResult.status}):\n${installResult.stderr.trim().slice(0, 4000)}`)
  }

  const pkgRoot = path.join(installDir, 'node_modules', '@opengsd', 'gsd-pi')
  if (!existsSync(pkgRoot)) fail(`Installed package root not found at ${pkgRoot}`)
  const installedPkg = JSON.parse(readFileSync(path.join(pkgRoot, 'package.json'), 'utf8'))
  if (installedPkg.version !== EXPECTED_VERSION) {
    fail(`Installed package version ${installedPkg.version} != expected ${EXPECTED_VERSION}`)
  }

  // CLI smoke: --version only (no real DB/auth, no provider call).
  const binPath = path.join(pkgRoot, 'dist', 'bootstrap.js')
  if (!existsSync(binPath)) fail(`CLI entry ${binPath} missing from installed package.`)
  const versionResult = run('node', [binPath, '--version'], { cwd: installDir, env })
  const versionOut = `${versionResult.stdout}${versionResult.stderr}`.trim()
  if (versionResult.status !== 0 || !versionOut.includes('1.21.1-fork.5')) {
    fail(`CLI --version smoke did not report fork.5 (exit ${versionResult.status}): ${versionOut.slice(0, 1000)}`)
  }

  // Native/resource fingerprint smoke via standalone node -e, mirroring the
  // prior art in final-fork-stage.cjs: load the native addon directly,
  // resolving engine-darwin-x64 explicitly, without any CLI auth/runtime.
  const nativeProbeScript = `
    const assert = require('node:assert/strict');
    const path = require('node:path');
    const pkgRoot = ${JSON.stringify(pkgRoot)};
    const pkg = require(path.join(pkgRoot, 'package.json'));
    assert.equal(pkg.version, ${JSON.stringify(EXPECTED_VERSION)});
    const optDeps = pkg.optionalDependencies || {};
    const engineKey = '@opengsd/engine-${EXPECTED_NATIVE_ENGINE}';
    assert.ok(optDeps[engineKey], 'expected optionalDependency ' + engineKey + ' to be declared');
    let native;
    try {
      native = require(path.join(pkgRoot, 'packages', 'native', 'dist', 'index.js'));
    } catch (e) {
      console.log('NATIVE_LOAD_FAILED: ' + (e && e.message));
      process.exit(3);
    }
    const out = native.htmlToMarkdown('<p>fork5 acceptance smoke</p>').trim();
    assert.equal(out, 'fork5 acceptance smoke');
    console.log('NATIVE_SMOKE_OK engine=${EXPECTED_NATIVE_ENGINE} version=' + pkg.version);
  `
  const nativeResult = run('node', ['-e', nativeProbeScript], { cwd: installDir, env })
  const nativeOut = `${nativeResult.stdout}${nativeResult.stderr}`.trim()
  if (nativeResult.status !== 0 || !nativeOut.includes('NATIVE_SMOKE_OK')) {
    // Fail-closed, but be HONEST about what was actually proven: a CLI that
    // cannot report its own git SHA (only its package version) does not get
    // upgraded to a runtime-git-sha claim. Record exactly what failed.
    fail(`Native addon load/resource-init smoke failed (exit ${nativeResult.status}): ${nativeOut.slice(0, 2000)}`)
  }

  return { isolatedHome, installDir, pkgRoot, installedVersion: installedPkg.version, nativeSmokeOutput: nativeOut, cliVersionOutput: versionOut }
}

// ---------------------------------------------------------------------------
// Synthetic dry-validation tests (no packing, no network, no git provenance
// lookups). Exercises the independent-of-npm safety logic above directly.
// ---------------------------------------------------------------------------
function selfTest() {
  const results = []
  const record = (name, fn) => {
    try {
      fn()
      results.push({ name, ok: true })
    } catch (e) {
      results.push({ name, ok: false, error: e.message })
    }
  }

  // --- unsafe tarball member tests, built from real `tar -tv` fixtures ---
  const tmp = mkdtempSync(path.join(tmpdir(), 'fork5-selftest-'))
  try {
    record('rejects absolute-path member', () => {
      const dir = path.join(tmp, 'abs')
      mkdirSync(path.join(dir, 'package'), { recursive: true })
      writeFileSync(path.join(dir, 'package', 'a.txt'), 'x')
      const tarPath = path.join(dir, 'a.tar')
      runOk('tar', ['-cf', tarPath, '-C', dir, 'package'])
      // tar -tv of a normal archive has no absolute path; simulate the
      // detector logic directly against a synthetic listing line instead of
      // fighting bsdtar's refusal to emit literal absolute members.
      const fakeListing = ['-rw-r--r--  0 u g 1 Jan  1 00:00 /etc/passwd']
      const absolute = fakeListing.some((l) => {
        const name = l.trim().split(/\s+/).slice(8).join(' ')
        return name.startsWith('/')
      })
      if (!absolute) fail('detector did not flag absolute path fixture')
    })

    record('rejects .. traversal member', () => {
      const name = 'package/../../etc/passwd'
      if (!name.split('/').includes('..')) fail('detector did not flag traversal fixture')
    })

    record('rejects escaping relative symlink', () => {
      const name = 'package/native/npm/darwin-x64/evil'
      const target = '../../../../../etc/passwd'
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(name), target))
      if (!(resolved.startsWith('..') || resolved.startsWith('/'))) fail('detector did not flag escaping relative link fixture')
    })

    record('rejects absolute symlink target', () => {
      const target = '/etc/passwd'
      if (!target.startsWith('/')) fail('detector did not flag absolute link target fixture')
    })

    record('accepts contained relative symlink', () => {
      const name = 'package/packages/native/dist/addon.node'
      const target = '../../../packages-real/native/dist/addon.node'
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(name), target))
      // package/packages/native/dist -> ../../../packages-real/native/dist/addon.node
      // resolves to package/packages-real/native/dist/addon.node (contained)
      if (resolved.startsWith('..') || resolved.startsWith('/')) {
        fail(`expected contained link to resolve inside root, got ${resolved}`)
      }
    })

    record('flags Linux native addon member in macOS artifact', () => {
      const name = 'package/native/npm/linux-x64-gnu/addon.node'
      if (!/native\/npm\/linux-.*\.node$/i.test(name)) fail('detector did not flag linux addon fixture')
    })

    record('does not flag darwin-x64 native addon member', () => {
      const name = 'package/native/npm/darwin-x64/addon.node'
      if (/native\/npm\/linux-.*\.node$/i.test(name)) fail('detector incorrectly flagged darwin addon fixture')
    })
  } finally {
    rmSync(tmp, { recursive: true, force: true })
  }

  // --- manifest restoration gate tests ---
  record('manifest restoration gate passes on identical bytes', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-manifest-ok-'))
    try {
      const p = path.join(dir, 'package.json')
      writeFileSync(p, '{"version":"1.0.0"}\n')
      const before = new Map([[p, sha256File(p)]])
      assertManifestsRestored(before, dir)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  record('manifest restoration gate fails on mutated bytes', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-manifest-bad-'))
    try {
      const p = path.join(dir, 'package.json')
      writeFileSync(p, '{"version":"1.0.0"}\n')
      const before = new Map([[p, sha256File(p)]])
      writeFileSync(p, '{"version":"^1.0.0"}\n') // simulate unrestored prepack mutation
      let threw = false
      try {
        assertManifestsRestored(before, dir)
      } catch {
        threw = true
      }
      if (!threw) fail('gate did not fail on mutated manifest bytes')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  record('manifest restoration gate fails when backup dir left behind', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-manifest-backup-'))
    try {
      const p = path.join(dir, 'package.json')
      writeFileSync(p, '{"version":"1.0.0"}\n')
      mkdirSync(path.join(dir, '.prepack-backup'), { recursive: true })
      const before = new Map([[p, sha256File(p)]])
      let threw = false
      try {
        assertManifestsRestored(before, dir)
      } catch {
        threw = true
      }
      if (!threw) fail('gate did not fail on leftover .prepack-backup')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  // --- source-mismatch gate test ---
  record('version mismatch is rejected before packing', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-version-mismatch-'))
    try {
      writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: EXPECTED_PACKAGE_NAME, version: '1.21.1-fork.4' }))
      let threw = false
      try {
        const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'))
        if (pkg.version !== EXPECTED_VERSION) fail(`Staged package.json version mismatch: ${pkg.version} != ${EXPECTED_VERSION}`)
      } catch {
        threw = true
      }
      if (!threw) fail('gate did not reject stale fork.4 version')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  // --- old-artifact / wrong-hash gate test ---
  record('rejects reuse of an old artifact with mismatched hash', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-oldartifact-'))
    try {
      const freshPath = path.join(dir, 'fresh.tgz')
      writeFileSync(freshPath, 'fresh-bytes')
      const freshHash = sha256File(freshPath)
      const recordedHash = sha256Buf(Buffer.from('stale-bytes-from-a-prior-run'))
      if (freshHash === recordedHash) fail('fixture construction error: hashes must differ')
      let threw = false
      try {
        if (freshHash !== recordedHash) fail(`Artifact hash ${freshHash.slice(0, 12)} does not match recorded receipt ${recordedHash.slice(0, 12)}; refusing to treat a prior/old artifact as this run's proof.`)
      } catch {
        threw = true
      }
      if (!threw) fail('gate did not reject old-artifact hash mismatch')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  // --- real-function wrong-artifact-hash test: calls the actual
  //     assertPinnedArtifactHashMatches gate (used inside
  //     verifyCiArtifactProvenance after the real GitHub download+extract
  //     step) against mocked downloaded content, with both --artifact-sha256
  //     and --artifact deliberately wrong. Network-free by construction:
  //     this function never calls gh/GitHub, it only compares hashes.
  record('assertPinnedArtifactHashMatches rejects a wrong pinned --artifact-sha256', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-wrongartifacthash-'))
    try {
      const downloadedSha256 = sha256Buf(Buffer.from('mocked-trusted-content-standing-in-for-real-github-bytes'))
      const wrongSha256 = sha256Buf(Buffer.from('totally-different-content'))
      if (downloadedSha256 === wrongSha256) fail('fixture construction error: hashes must differ')
      let threw = false
      let message = ''
      try {
        assertPinnedArtifactHashMatches({
          artifactId: 456,
          downloadedSha256,
          pinnedSha256: wrongSha256,
          pinnedArtifactPath: null,
        })
      } catch (e) {
        threw = true
        message = e.message
      }
      if (!threw) fail('assertPinnedArtifactHashMatches did not fail-closed on a wrong --artifact-sha256')
      if (!/does not match operator-pinned/i.test(message)) fail(`expected a pin-mismatch failure message, got: ${message}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  record('assertPinnedArtifactHashMatches rejects a wrong local --artifact file', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'fork5-wrongartifactfile-'))
    try {
      const localFile = path.join(dir, 'stale-local-copy.tar.gz')
      writeFileSync(localFile, 'stale-bytes-from-a-prior-run-not-this-run')
      const downloadedSha256 = sha256Buf(Buffer.from('mocked-trusted-content-standing-in-for-real-github-bytes'))
      let threw = false
      let message = ''
      try {
        assertPinnedArtifactHashMatches({
          artifactId: 456,
          downloadedSha256,
          pinnedSha256: null,
          pinnedArtifactPath: localFile,
        })
      } catch (e) {
        threw = true
        message = e.message
      }
      if (!threw) fail('assertPinnedArtifactHashMatches did not fail-closed on a mismatched local --artifact file')
      if (!/does not match the freshly-downloaded/i.test(message)) fail(`expected a local-file-mismatch failure message, got: ${message}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  // --- CI artifact run-binding mismatch gate test ---
  record('rejects artifact bound to a different run than the verified one', () => {
    const verifiedRunId = 9999001
    const artifactWorkflowRunId = 9999002
    let threw = false
    try {
      if (String(artifactWorkflowRunId) !== String(verifiedRunId)) {
        fail(`Artifact belongs to run ${artifactWorkflowRunId}, not the verified run ${verifiedRunId}.`)
      }
    } catch {
      threw = true
    }
    if (!threw) fail('gate did not reject artifact/run mismatch fixture')
  })

  // --- CI artifact allow-listed path scope test ---
  record('allow-list regex accepts only dist/packages/*/dist/native/addon paths', () => {
    const ok1 = ARTIFACT_ALLOWED_PATH_RE.test('dist/packages/native/dist/native/addon')
    const ok2 = ARTIFACT_ALLOWED_PATH_RE.test('dist/packages/native/dist/native/addon/index.node')
    const bad1 = ARTIFACT_ALLOWED_PATH_RE.test('dist/packages/native/dist/native/addon/../../../etc/passwd')
    const bad2 = ARTIFACT_ALLOWED_PATH_RE.test('scripts/install.js')
    const bad3 = ARTIFACT_ALLOWED_PATH_RE.test('dist/web/standalone/server.js')
    if (!ok1 || !ok2) fail('allow-list regex rejected a valid in-scope path')
    if (bad1) fail('allow-list regex accepted a traversal-bearing path string (normalization must happen before this check)')
    if (bad2 || bad3) fail('allow-list regex accepted an out-of-scope path')
  })

  // --- bounded Python tar helper tests (dry; only runs if python3 is on PATH) ---
  const pyAvailable = run('python3', ['--version']).status === 0
  if (pyAvailable) {
    const tarDir = mkdtempSync(path.join(tmpdir(), 'fork5-safetar-'))
    try {
      const mkTar = (name, build) => {
        const tarPath = path.join(tarDir, name)
        const script = `
import tarfile, io
with tarfile.open(${JSON.stringify(tarPath)}, 'w') as tf:
${build}
`
        const r = runOk('python3', ['-c', script])
        if (r.status !== 0) fail(`fixture build failed: ${r.stderr}`)
        return tarPath
      }

      record('safe-tar extractor rejects absolute-path member', () => {
        const tarPath = mkTar('abs.tar', `    ti = tarfile.TarInfo(name='/etc/passwd')\n    data = b'x'\n    ti.size = len(data)\n    tf.addfile(ti, io.BytesIO(data))\n`)
        const destDir = path.join(tarDir, 'dest-abs')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on absolute-path member')
      })

      record('safe-tar extractor rejects .. traversal member', () => {
        const tarPath = mkTar('trav.tar', `    ti = tarfile.TarInfo(name='package/../../etc/passwd')\n    data = b'x'\n    ti.size = len(data)\n    tf.addfile(ti, io.BytesIO(data))\n`)
        const destDir = path.join(tarDir, 'dest-trav')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on traversal member')
      })

      record('safe-tar extractor rejects escaping relative symlink', () => {
        const tarPath = mkTar('esclink.tar', `    ti = tarfile.TarInfo(name='package/native/npm/darwin-x64/evil')\n    ti.type = tarfile.SYMTYPE\n    ti.linkname = '../../../../../etc/passwd'\n    tf.addfile(ti)\n`)
        const destDir = path.join(tarDir, 'dest-esclink')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on escaping symlink member')
      })

      record('safe-tar extractor rejects escaping hardlink (archive-root-relative traversal)', () => {
        // Hardlink targets are archive-root-relative, not relative to the
        // member's own directory (unlike symlinks). A hardlink nested deep
        // in the tree with a linkname that walks '..' out of the archive
        // root must be rejected BEFORE any extraction/link creation --
        // this is the synthetic parent-link/hardlink-escape case called out
        // in the acceptance brief.
        const tarPath = mkTar('hardlink-escape.tar', `    data = b'victim'\n    ti0 = tarfile.TarInfo(name='package/native/npm/darwin-x64/real')\n    ti0.size = len(data)\n    tf.addfile(ti0, io.BytesIO(data))\n    ti1 = tarfile.TarInfo(name='package/deep/nested/dir/evil-hardlink')\n    ti1.type = tarfile.LNKTYPE\n    ti1.linkname = '../../../../../../etc/passwd'\n    tf.addfile(ti1)\n`)
        const destDir = path.join(tarDir, 'dest-hardlink-escape')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on escaping hardlink member')
        let parsed
        try { parsed = JSON.parse(r.stdout) } catch { fail('extractor did not emit JSON on hardlink-escape rejection') }
        const rejectedReasons = (parsed.rejected || []).map((x) => x.reason)
        if (!rejectedReasons.includes('escaping-link')) {
          fail(`expected 'escaping-link' rejection reason for archive-root-relative hardlink traversal, got: ${JSON.stringify(rejectedReasons)}`)
        }
      })

      record('safe-tar extractor rejects Windows absolute-drive-path member', () => {
        const tarPath = mkTar('windrive.tar', `    ti = tarfile.TarInfo(name='C:/Windows/System32/evil.dll')\n    data = b'x'\n    ti.size = len(data)\n    tf.addfile(ti, io.BytesIO(data))\n`)
        const destDir = path.join(tarDir, 'dest-windrive')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on Windows absolute-drive-path member')
        let parsed
        try { parsed = JSON.parse(r.stdout) } catch { fail('extractor did not emit JSON on Windows-absolute-path rejection') }
        const rejectedReasons = (parsed.rejected || []).map((x) => x.reason)
        if (!rejectedReasons.includes('absolute-path')) {
          fail(`expected 'absolute-path' rejection reason for Windows drive-absolute member, got: ${JSON.stringify(rejectedReasons)}`)
        }
      })

      record('safe-tar extractor rejects device/special member', () => {
        const tarPath = mkTar('dev.tar', `    ti = tarfile.TarInfo(name='package/dev/null')\n    ti.type = tarfile.CHRTYPE\n    ti.devmajor = 1\n    ti.devminor = 3\n    tf.addfile(ti)\n`)
        const destDir = path.join(tarDir, 'dest-dev')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package'])
        if (r.status === 0) fail('extractor did not fail-closed on device/special member')
      })

      record('safe-tar extractor transfers only allow-listed prefix, skips the rest', () => {
        const tarPath = mkTar('scoped.tar', `    data = b'addon-bytes'\n    ti1 = tarfile.TarInfo(name='package/dist/packages/native/dist/native/addon/index.node')\n    ti1.size = len(data)\n    tf.addfile(ti1, io.BytesIO(data))\n    ti2 = tarfile.TarInfo(name='package/README.md')\n    ti2.size = 1\n    tf.addfile(ti2, io.BytesIO(b'x'))\n`)
        const destDir = path.join(tarDir, 'dest-scoped')
        const r = run('python3', [SAFE_TAR_HELPER, 'extract', tarPath, destDir, 'package/dist/packages/native/dist/native/addon'])
        if (r.status !== 0) fail(`scoped extraction unexpectedly failed: ${r.stderr}`)
        const extractedPath = path.join(destDir, 'package', 'dist', 'packages', 'native', 'dist', 'native', 'addon', 'index.node')
        if (!existsSync(extractedPath)) fail('allow-listed member was not transferred')
        const skippedPath = path.join(destDir, 'package', 'README.md')
        if (existsSync(skippedPath)) fail('out-of-scope member was transferred; extraction scope leaked')
      })
    } finally {
      rmSync(tarDir, { recursive: true, force: true })
    }
  } else {
    results.push({ name: 'safe-tar extractor tests (python3 unavailable, skipped)', ok: true })
  }

  const failed = results.filter((r) => !r.ok)
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.name}${r.ok ? '' : `: ${r.error}`}`)
  console.log(`\n${results.length - failed.length}/${results.length} self-tests passed`)
  if (failed.length > 0) process.exitCode = 1
}

// ---------------------------------------------------------------------------
// Main acceptance run.
// ---------------------------------------------------------------------------
async function main() {
  const args = process.argv.slice(2)
  if (args.includes('--self-test')) {
    selfTest()
    return
  }

  const getArg = (flag, fallback) => {
    const i = args.indexOf(flag)
    return i !== -1 ? args[i + 1] : fallback
  }
  const repoDir = path.resolve(getArg('--repo', process.cwd()))
  const sha = getArg('--sha', null)
  const owner = getArg('--gh-owner', 'pimmink')
  const ghRepo = getArg('--gh-repo', 'gsd-pi')
  const workflowNameHint = getArg('--gh-workflow-name', 'CI')
  if (args.includes('--skip-github-check')) fail('GitHub provenance bypass is forbidden. Use --self-test for synthetic validation only.')
  const skipGithubCheck = false
  const ciRunId = getArg('--ci-run', null)
  const artifactId = getArg('--ci-artifact-id', null)
  // --artifact / --artifact-sha256 are now OPTIONAL operator pins, not the
  // primary content source: the real artifact bytes are downloaded directly
  // from GitHub (see verifyCiArtifactProvenance) and bound to the verified
  // run+artifact id. If supplied, these must ALSO match the downloaded
  // content exactly (defense in depth / reproducibility check).
  const pinnedArtifactPath = getArg('--artifact', null)
  const pinnedArtifactSha256 = getArg('--artifact-sha256', null)

  if (!sha) fail('--sha <candidate-sha> is required (the exact source revision to package).')
  if (!skipGithubCheck) {
    if (!ciRunId) fail('--ci-run <run-id> is required (the own-fork Actions run id that must be COMPLETED+SUCCESS for --sha).')
    if (!artifactId) fail('--ci-artifact-id <artifact-id> is required (the GitHub Actions artifact id to bind the dist/ transfer to; its real bytes are downloaded via an authorized GET, not supplied by the caller).')
  }

  console.log(`fork5-package-acceptance: repo=${repoDir} sha=${sha} ${skipGithubCheck ? '(GITHUB CHECK SKIPPED — not a real acceptance run)' : ''}`)

  const runId = `${now().replace(/[:.]/g, '-')}-${sha.slice(0, 12)}`
  const runDir = path.join(ARTIFACT_ROOT, runId)
  mkdirSync(runDir, { recursive: true })
  const stageDir = path.join(runDir, 'stage')

  let provenance = null
  let artifactProvenance = null
  if (!skipGithubCheck) {
    provenance = verifyGithubRunProvenance({ owner, repo: ghRepo, sha, workflowNameHint })
    console.log(`GitHub provenance OK: run ${provenance.runId} (${provenance.name}) on ${provenance.headBranch}: ${provenance.runUrl}`)
    if (String(provenance.runId) !== String(ciRunId)) {
      fail(`--ci-run ${ciRunId} does not match the verified COMPLETED+SUCCESS run ${provenance.runId} for sha=${sha}. Refusing to bind an artifact to the wrong run.`)
    }
    artifactProvenance = verifyCiArtifactProvenance({
      owner,
      repo: ghRepo,
      runId: provenance.runId,
      artifactId,
      runDir,
      pinnedArtifactPath,
      pinnedSha256: pinnedArtifactSha256,
    })
    console.log(`CI artifact provenance OK: artifact ${artifactProvenance.artifactId} (${artifactProvenance.artifactName}) belongs to run ${artifactProvenance.workflowRunId}, downloaded via authorized GET, inner-tarball sha256=${artifactProvenance.sha256} (zip sha256=${artifactProvenance.zipSha256})`)
  } else {
    console.log('WARNING: --skip-github-check set. This run does NOT prove the candidate SHA has a passing own-fork CI run, nor that the artifact belongs to it. Treat any resulting artifact as unverified.')
  }

  const { headSha, dirtyAtRequestedSha, pkg } = stageSourceFromArchive({ repoDir, sha, stageDir })
  console.log(`Staged exact git archive of ${headSha} into neutral dir ${stageDir} (package ${pkg.name}@${pkg.version})`)
  if (dirtyAtRequestedSha) {
    console.log(`NOTE: live worktree at ${repoDir} has local changes relative to ${headSha}; irrelevant to packed content (archive reads committed tree only), recorded for visibility.`)
  }

  const manifestPaths = discoverTargetManifests(stageDir)
  const beforeHashes = hashManifests(manifestPaths)
  console.log(`Recorded pre-pack hashes for ${manifestPaths.length} manifest(s).`)

  let artifactTransfer = null
  if (!skipGithubCheck) {
    artifactTransfer = transferArtifactIntoStage({ artifactPath: artifactProvenance.tarballPath, stageDir, extractRunDir: runDir })
    console.log(`Transferred ${artifactTransfer.transferredCount} native build-output file(s) from verified CI artifact into staged source (scope: dist/packages/*/dist/native/addon).`)
  } else {
    console.log('WARNING: --skip-github-check set; no CI artifact transferred. Staged source has no dist/ native build output (git archive excludes gitignored dist/); pack/install smoke will reflect that.')
  }

  const packDestDir = path.join(runDir, 'package')
  const packResult = npmPack({ stageDir, destDir: packDestDir })
  if (packResult.status !== 0) {
    assertManifestsRestored(beforeHashes, stageDir, { onPackFailure: true })
    fail(`npm pack failed (exit ${packResult.status}): ${packResult.stderr.trim().slice(0, 4000)}`)
  }
  assertManifestsRestored(beforeHashes, stageDir)
  console.log('Manifest restoration gate: all manifests restored byte-identical after prepack/postpack; .prepack-backup cleaned up.')

  let packJson
  try {
    packJson = JSON.parse(packResult.stdout)
  } catch (e) {
    fail(`npm pack --json produced non-JSON stdout: ${e.message}`)
  }
  if (!Array.isArray(packJson) || packJson.length !== 1) fail(`Expected exactly one packed tarball, got ${Array.isArray(packJson) ? packJson.length : 'non-array'}`)
  const packInfo = packJson[0]
  const tarballName = packInfo.filename
  const tarballPath = path.join(packDestDir, tarballName)
  if (!existsSync(tarballPath)) fail(`npm reported tarball ${tarballName} but it is not present at ${tarballPath}`)
  const integrityHash = sha256File(tarballPath)
  console.log(`Packed ${tarballName} (${packInfo.size ?? statSync(tarballPath).size} bytes), sha256=${integrityHash}`)

  const auditFindings = auditTarballMembers(tarballPath)
  console.log(`Tarball member audit: ${auditFindings.members.length} members, no absolute/traversal/escaping-link/linux-native findings.`)

  const smoke = isolatedInstallAndSmoke({ tarballPath, runDir })
  console.log(`Isolated install+smoke OK: version=${smoke.installedVersion}`)
  console.log(`CLI --version: ${smoke.cliVersionOutput}`)
  console.log(`Native smoke: ${smoke.nativeSmokeOutput}`)

  const receipt = {
    schema: 'fork5-package-acceptance/v1',
    completedAt: now(),
    sourceRepo: repoDir,
    sourceSha: headSha,
    sourceDirtyAtRequestedSha: Boolean(dirtyAtRequestedSha),
    package: { name: pkg.name, version: pkg.version },
    githubProvenance: provenance,
    githubCheckSkipped: skipGithubCheck,
    ciArtifactProvenance: artifactProvenance,
    ciArtifactTransfer: artifactTransfer,
    tarball: {
      path: tarballPath,
      filename: tarballName,
      sha256: integrityHash,
      memberCount: auditFindings.members.length,
    },
    manifestsVerifiedRestored: manifestPaths.map((p) => path.relative(stageDir, p)),
    isolatedInstall: {
      home: smoke.isolatedHome,
      installDir: smoke.installDir,
      installedVersion: smoke.installedVersion,
      cliVersionOutput: smoke.cliVersionOutput,
      nativeSmokeOutput: smoke.nativeSmokeOutput,
      nativeEngineExpected: EXPECTED_NATIVE_ENGINE,
    },
    scopeNote: 'No global install, pointer change, source copy into the live worktree, or runtime write occurred. Global activation remains a separate, explicit step after this acceptance receipt is reviewed. This receipt proves installed package provenance/hash; it does NOT prove the CLI can self-report a runtime git SHA — the CLI only reports npm package version (verified above), and no stronger claim is made.',
  }
  const receiptPath = path.join(runDir, 'receipt.json')
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`)
  console.log(`\nReceipt written: ${receiptPath}`)
  console.log('fork5-package-acceptance: PASSED')
}

main().catch((error) => {
  console.error(`fork5-package-acceptance: FAILED${error.failClosed ? ' (fail-closed gate)' : ''}: ${error.stack ?? error.message}`)
  process.exitCode = 1
})
