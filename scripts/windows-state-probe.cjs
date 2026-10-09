// Diagnostic-only instrumentation for the STATE.md render Windows timeout
// investigation (D221: /tmp/fork5-90d-windows-validation.log shows the 180s
// windows_mcp_only process budget exhausted mid-run with four STATE.md
// render cases at 49.37s / 8.48s / 34.02s / 17.12s before timeout).
//
// This file ONLY instruments; it never edits production source. It targets
// exactly one existing, uninstrumented, unchanged test file
// (dist-test/packages/mcp-server/src/state-md-render.test.js) and attributes
// its wall-clock time across three causal buckets, aggregated only (never
// per-call) to avoid adding its own IO overhead to the budget it is
// measuring:
//   - child_process (execFileSync/spawnSync), split into GIT / PowerShell /
//     other by command basename only (never args, return values, or env)
//   - synchronous fs entry points (existsSync/readFileSync/writeFileSync/
//     statSync/mkdirSync/appendFileSync/openSync/closeSync/renameSync/
//     rmSync/copyFileSync), counted + timed in aggregate
//   - best-effort workflow-logger phase labels, read from the in-process
//     log buffer if that module is reachable, reported as counts only
//
// Usage (parent process only):
//   node windows-state-probe.cjs <dist-test-dir-or-file> [--test-name-pattern=<regex>] [--concurrency=1]
//
// Unchanged 180000ms process budget. Runs ONLY the one file via
// --test-name-pattern targeting, or --test-concurrency to force serial
// diagnostic isolation of a single named case; never substitutes for or
// skips the canonical contiguous run elsewhere in the harness.
const cp = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const { syncBuiltinESMExports } = require("node:module");
const { resolve, join, basename } = require("node:path");
const { performance } = require("node:perf_hooks");

const SYNC_FS_METHODS = [
  "existsSync",
  "readFileSync",
  "writeFileSync",
  "statSync",
  "lstatSync",
  "mkdirSync",
  "appendFileSync",
  "openSync",
  "closeSync",
  "renameSync",
  "rmSync",
  "rmdirSync",
  "copyFileSync",
  "readdirSync",
  "unlinkSync",
];

if (require.main === module) {
  const target = resolve(process.argv[2]);
  const namePatternArg = process.argv.find((a) => a.startsWith("--test-name-pattern="));
  const concurrencyArg = process.argv.find((a) => a.startsWith("--concurrency="));
  const testNamePattern = namePatternArg
    ? namePatternArg.slice("--test-name-pattern=".length)
    : undefined;
  const testConcurrency = concurrencyArg
    ? concurrencyArg.slice("--concurrency=".length)
    : undefined;

  const targetIsDir = fs.statSync(target).isDirectory();
  const targets = targetIsDir
    ? fs
        .readdirSync(target, { recursive: true })
        .filter((name) => name.endsWith("state-md-render.test.js"))
        .sort()
        .map((name) => join(target, name))
    : [target];
  if (!targets.length) throw new Error("No state-md-render.test.js file found under target");
  if (targets.length > 1)
    throw new Error(`Expected exactly one state-md-render.test.js, found ${targets.length}`);

  const availableParallelism =
    typeof os.availableParallelism === "function" ? os.availableParallelism() : "unavailable";
  const cpuCount = os.cpus().length;
  const nodeDefaultConcurrency =
    typeof availableParallelism === "number"
      ? Math.max(1, availableParallelism - 1)
      : "unknown (no availableParallelism)";
  console.error(
    `[STATE-PROBE] cpuCount=${cpuCount} availableParallelism=${availableParallelism} ` +
      `nodeDefaultConcurrencyIfUnset=${nodeDefaultConcurrency} chosenConcurrency=${testConcurrency ?? "unset (node default applies)"}`,
  );
  console.error(`[STATE-PROBE] selected file=${targets[0]}`);
  if (testNamePattern) console.error(`[STATE-PROBE] test-name-pattern=${testNamePattern}`);

  const profileDir = resolve("state-diagnostic-profiles");
  fs.mkdirSync(profileDir, { recursive: true });

  const args = ["--test", "--test-reporter=tap"];
  if (testConcurrency !== undefined) args.push(`--test-concurrency=${testConcurrency}`);
  if (testNamePattern) args.push(`--test-name-pattern=${testNamePattern}`);
  args.push(...targets);

  const start = performance.now();
  const r = cp.spawnSync(process.execPath, args, {
    env: {
      ...process.env,
      STATE_PROFILE_DIR: profileDir,
      NODE_OPTIONS: `${process.env.NODE_OPTIONS || ""} --require=${JSON.stringify(__filename.replaceAll("\\", "/"))}`,
    },
    encoding: "utf8",
    timeout: 180000,
    maxBuffer: 16 * 1024 * 1024,
  });
  const wallMs = performance.now() - start;

  process.stdout.write(r.stdout || "");
  process.stderr.write(r.stderr || "");

  let attributedMs = 0;
  for (const name of fs.readdirSync(profileDir)) {
    const raw = fs.readFileSync(join(profileDir, name), "utf8").trim();
    if (!raw) continue;
    console.error(`[STATE-PROBE] summary file=${name}`);
    console.error(raw);
    const match = raw.match(/childProcessTotalMs=([\d.]+).*syncFsTotalMs=([\d.]+)/s);
    if (match) attributedMs += Number(match[1]) + Number(match[2]);
  }
  const unattributedMs = wallMs - attributedMs;
  console.error(
    `[STATE-PROBE] wallMs=${wallMs.toFixed(1)} attributedMs=${attributedMs.toFixed(1)} ` +
      `unattributedMs=${unattributedMs.toFixed(1)} (unattributed includes SQLite/engine/JS work this probe cannot causally attribute)`,
  );
  console.error(
    `[STATE-PROBE] unchanged 180000ms process budget; status=${r.status} signal=${r.signal} error=${r.error?.code || "none"}`,
  );
  process.exitCode = r.status === 0 && !r.error && !r.signal ? 0 : 1;
} else {
  // Preload context (inside the --test child process). Instrument only;
  // never alter command, args, cwd, env, or return values seen by callers.
  const counters = {
    childProcess: {
      GIT: { calls: 0, ms: 0 },
      PowerShell: { calls: 0, ms: 0 },
      other: { calls: 0, ms: 0 },
    },
    syncFs: { calls: 0, ms: 0, byMethod: {} },
    phaseLabels: {},
  };

  const categorize = (command) => {
    const base = basename(String(command)).toLowerCase();
    if (base === "git" || base === "git.exe") return "GIT";
    if (base === "powershell.exe" || base === "powershell") return "PowerShell";
    return "other";
  };

  const wrapChildProcessFn = (original) => {
    return function (command, ...rest) {
      const category = categorize(command);
      const start = performance.now();
      try {
        return original.call(this, command, ...rest);
      } finally {
        const ms = performance.now() - start;
        counters.childProcess[category].calls += 1;
        counters.childProcess[category].ms += ms;
      }
    };
  };

  const originalExecFileSync = cp.execFileSync;
  const originalSpawnSync = cp.spawnSync;
  const originalExecSync = cp.execSync;
  cp.execFileSync = wrapChildProcessFn(originalExecFileSync);
  cp.spawnSync = wrapChildProcessFn(originalSpawnSync);
  // execSync(command, options) takes a full command string, not (command, args);
  // categorize on the leading token instead of basename(command).
  cp.execSync = function (command, ...rest) {
    const leading = String(command).trim().split(/\s+/)[0] || "";
    const category = categorize(leading);
    const start = performance.now();
    try {
      return originalExecSync.call(this, command, ...rest);
    } finally {
      const ms = performance.now() - start;
      counters.childProcess[category].calls += 1;
      counters.childProcess[category].ms += ms;
    }
  };

  for (const methodName of SYNC_FS_METHODS) {
    const original = fs[methodName];
    if (typeof original !== "function") continue;
    fs[methodName] = function (...args) {
      const start = performance.now();
      try {
        return original.apply(this, args);
      } finally {
        const ms = performance.now() - start;
        counters.syncFs.calls += 1;
        counters.syncFs.ms += ms;
        counters.syncFs.byMethod[methodName] = (counters.syncFs.byMethod[methodName] || 0) + 1;
      }
    };
  }

  // Best-effort: if the workflow-logger module is already loaded by the test
  // (it is, transitively, via gsd-db and friends), snapshot its in-memory
  // buffer at process exit to get phase/component labels for free. This is
  // read-only introspection of an existing export; it adds no new call sites
  // to production code and never mutates the buffer.
  process.once("exit", () => {
    try {
      const candidates = Object.keys(require.cache).filter((key) =>
        key.endsWith("workflow-logger.js"),
      );
      for (const key of candidates) {
        const mod = require.cache[key]?.exports;
        if (mod && typeof mod.getLogs === "function") {
          for (const entry of mod.getLogs()) {
            const label = entry?.component || "unknown";
            counters.phaseLabels[label] = (counters.phaseLabels[label] || 0) + 1;
          }
        }
      }
    } catch {
      // Best-effort only; absence of this module/export must not fail the probe.
    }

    const childProcessTotalMs = Object.values(counters.childProcess).reduce(
      (sum, c) => sum + c.ms,
      0,
    );
    const lines = [
      `[STATE-PROBE] pid=${process.pid} childProcessTotalMs=${childProcessTotalMs.toFixed(1)} syncFsTotalMs=${counters.syncFs.ms.toFixed(1)}`,
      `[STATE-PROBE] pid=${process.pid} childProcess GIT calls=${counters.childProcess.GIT.calls} ms=${counters.childProcess.GIT.ms.toFixed(1)}`,
      `[STATE-PROBE] pid=${process.pid} childProcess PowerShell calls=${counters.childProcess.PowerShell.calls} ms=${counters.childProcess.PowerShell.ms.toFixed(1)}`,
      `[STATE-PROBE] pid=${process.pid} childProcess other calls=${counters.childProcess.other.calls} ms=${counters.childProcess.other.ms.toFixed(1)}`,
      `[STATE-PROBE] pid=${process.pid} syncFs calls=${counters.syncFs.calls} ms=${counters.syncFs.ms.toFixed(1)} byMethod=${JSON.stringify(counters.syncFs.byMethod)}`,
      `[STATE-PROBE] pid=${process.pid} phaseLabels=${JSON.stringify(counters.phaseLabels)}`,
      `[STATE-PROBE] pid=${process.pid} nodeSqliteNativeAddon isNativeAddonLoaded=${isNodeSqliteLoaded()}`,
    ];
    if (process.env.STATE_PROFILE_DIR) {
      try {
        fs.writeFileSync(
          join(process.env.STATE_PROFILE_DIR, `${process.pid}.log`),
          `${lines.join("\n")}\n`,
        );
      } catch {
        // Diagnostic write failure must not affect test exit status.
      }
    }
    for (const line of lines) process.stderr.write(`${line}\n`);
  });

  const isNodeSqliteLoaded = () => {
    // Read-only check of whether node:sqlite was already require()'d by this
    // process (production loads it via createRequire(...).require("node:sqlite")
    // in db-provider.ts). Best-effort, non-invasive: no private V8/process
    // bindings, no secrets, no behavior change if the key is absent.
    try {
      return Object.keys(require.cache).includes("node:sqlite");
    } catch {
      return "unknown";
    }
  };

  syncBuiltinESMExports();
}
