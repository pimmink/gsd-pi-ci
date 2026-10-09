// Causal diagnostic only: attribute wall-clock time to @gsd/native(.node)
// addon calls, node:sqlite DatabaseSync/StatementSync calls, and process
// phases during the Windows STATE.md render test file, which the prior
// probe (windows-state-probe.cjs) could not explain - its
// unattributedMs=29341.2 (of wallMs=30902.4) covers everything the sync
// fs/child_process wrappers do not see, including native addon calls and
// SQLite/engine waiting. This file closes that gap for both the native
// addon side and the node:sqlite side, and separately buckets
// fixture/mutation/expectedState/render phases using markers the test file
// itself can emit (best-effort; absent markers degrade to "unphased", never
// a hard failure).
//
// SQLite instrumentation (2026-10-09 extension): the prior native-call pass
// (run 37886693142) proved @gsd/native addon calls account for only ~1.9s of
// the 83.9s STATE test file - the addon layer is not the bottleneck.
// Production loads SQLite via a bare `createRequire(...).require("node:sqlite")`
// (unit-ownership.ts's tryRequireNodeSqlite), which resolves as the literal
// string "node:sqlite" at Module._load - confirmed locally (unlike the
// @gsd/native case, this is a builtin module id, never a resolved
// filesystem path under any import shape). The wrapper below intercepts
// exactly that id, wraps DatabaseSync (constructor/exec/prepare/close) and
// the StatementSync instances prepare() returns (run/get/all), and records
// counts + wall/max time only - never SQL text, parameters, bound values,
// row data, or file paths. Mirrors the native-call wrapper's shape
// (Map-based call stats, phase-label bucketing, one summary line per
// call-site on process exit) so logs read identically.
//
// Mechanism: proxy Module._load (node:module's CJS loader entry point).
// state-md-render.test.ts is compiled/loaded as ESM, and its
// `import { syncDirectoryEntry } from "@gsd/native/directory-sync"` goes
// through Node's dual CJS/ESM module system - `@gsd/native` is a CommonJS
// package (`"type": "commonjs"`), so the ESM loader's CJS-interop path still
// calls the classic Module._load() internally to actually instantiate it.
// The request Module._load receives at that point is the fully RESOLVED
// filesystem path (e.g. ".../node_modules/@gsd/native/dist/directory-sync.js"),
// not the bare "@gsd/native/directory-sync" specifier string - confirmed by a
// standalone repro (a startsWith() match against the bare specifier silently
// never fires for an ESM-imported CJS dependency; an includes() match against
// the resolved path fires every time). The matcher below is `includes()` for
// exactly this reason.
//
// The returned export object's own property descriptors are read with
// Object.getOwnPropertyDescriptor and reinstalled unchanged (value, writable,
// enumerable, configurable) except that function values are replaced with a
// timing wrapper that calls straight through to the original - this
// preserves `typeof x === "function"`, `.length`, `.name` (via
// Object.defineProperty with the same descriptor shape) and avoids breaking
// instanceof/class-export patterns elsewhere in the module (e.g.
// ProjectionRootIdentityLock, SqliteFileIdentityLock are classes/
// constructors - those are left completely untouched by only wrapping
// descriptors whose value is a plain function, never anything used with
// `new`). No .node internals, no N-API boundary code, and no production
// source file are modified; this is pure require-time interception from a
// --require preload, exactly like windows-mcp-profile.cjs and
// windows-state-probe.cjs before it.
//
// Usage (parent process, same contract as windows-state-probe.cjs):
//   node windows-native-call-profile.cjs <dist-test-dir-or-file> \
//     [--test-name-pattern=<regex>] [--concurrency=1] [--relocate-temp=<dir>]
//
// --relocate-temp=<dir> is an opt-in diagnostic: when set, the child
// process's TEMP/TMP (and TMPDIR, for completeness on non-Windows) env vars
// are overridden to <dir> instead of inheriting the parent's. This exists to
// paired-compare the default C: runner temp against the existing
// RUNNER_TEMP directory on D: on Windows runners, without touching any
// production default or pragma. Absent this flag, TEMP/TMP are inherited
// unchanged exactly as before.
//
// Unchanged 180000ms process budget. Diagnostic-only; never substitutes for
// or skips the canonical run.
const Module = require("node:module");
const cp = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const { resolve, join, basename } = require("node:path");
const { performance } = require("node:perf_hooks");

const NATIVE_SPECIFIER_PREFIX = "@gsd/native";
// gsd-pi's own test/build loader (scripts/dist-test-resolve.mjs) resolves the
// @gsd/native workspace package to a repo-relative filesystem path that does
// NOT contain the "@gsd/native" substring at all - e.g.
// ".../dist-test/packages/native/dist/directory-sync/index.js" or
// ".../packages/native/dist/directory-sync/index.js" (built-package variant,
// see shouldUseBuiltPackageDist() in that loader). A second, independent
// pattern catches this workspace-alias resolution shape; either pattern
// matching is sufficient to flag and wrap the module.
const NATIVE_WORKSPACE_PATH_PATTERN = /[\/\\]packages[\/\\]native[\/\\]dist[\/\\]/;

if (require.main === module) {
  const target = resolve(process.argv[2]);
  const namePatternArg = process.argv.find((a) => a.startsWith("--test-name-pattern="));
  const concurrencyArg = process.argv.find((a) => a.startsWith("--concurrency="));
  const relocateTempArg = process.argv.find((a) => a.startsWith("--relocate-temp="));
  const testNamePattern = namePatternArg ? namePatternArg.slice("--test-name-pattern=".length) : undefined;
  const testConcurrency = concurrencyArg ? concurrencyArg.slice("--concurrency=".length) : undefined;
  const relocateTempDir = relocateTempArg ? relocateTempArg.slice("--relocate-temp=".length) : undefined;
  if (relocateTempDir) fs.mkdirSync(relocateTempDir, { recursive: true });

  const targetIsDir = fs.statSync(target).isDirectory();
  const targets = targetIsDir
    ? fs
        .readdirSync(target, { recursive: true })
        .filter((name) => name.endsWith("state-md-render.test.js"))
        .sort()
        .map((name) => join(target, name))
    : [target];
  if (!targets.length) throw new Error("No state-md-render.test.js file found under target");
  if (targets.length > 1) throw new Error(`Expected exactly one state-md-render.test.js, found ${targets.length}`);

  const availableParallelism =
    typeof os.availableParallelism === "function" ? os.availableParallelism() : "unavailable";
  const cpuCount = os.cpus().length;
  console.error(
    `[NATIVE-PROFILE] cpuCount=${cpuCount} availableParallelism=${availableParallelism} chosenConcurrency=${testConcurrency ?? "unset (node default applies)"}`,
  );
  console.error(`[NATIVE-PROFILE] selected file=${targets[0]}`);
  console.error(`[NATIVE-PROFILE] os.tmpdir()=${os.tmpdir()}`);
  console.error(`[NATIVE-PROFILE] RUNNER_TEMP=${process.env.RUNNER_TEMP || "(unset)"}`);
  console.error(`[NATIVE-PROFILE] TEMP(baseline)=${process.env.TEMP || "(unset)"} TMP(baseline)=${process.env.TMP || "(unset)"}`);
  if (relocateTempDir) {
    console.error(`[NATIVE-PROFILE] relocate-temp active: child TEMP/TMP/TMPDIR=${relocateTempDir}`);
  } else {
    console.error(`[NATIVE-PROFILE] relocate-temp not set; child inherits parent TEMP/TMP unchanged`);
  }
  if (testNamePattern) console.error(`[NATIVE-PROFILE] test-name-pattern=${testNamePattern}`);

  const profileDir = resolve("native-call-diagnostic-profiles");
  fs.mkdirSync(profileDir, { recursive: true });

  const args = ["--test", "--test-reporter=tap"];
  if (testConcurrency !== undefined) args.push(`--test-concurrency=${testConcurrency}`);
  if (testNamePattern) args.push(`--test-name-pattern=${testNamePattern}`);
  args.push(...targets);

  const start = performance.now();
  const r = cp.spawnSync(process.execPath, args, {
    env: {
      ...process.env,
      NATIVE_PROFILE_DIR: profileDir,
      NODE_OPTIONS: `${process.env.NODE_OPTIONS || ""} --require=${JSON.stringify(__filename.replaceAll("\\", "/"))}`,
      ...(relocateTempDir
        ? { TEMP: relocateTempDir, TMP: relocateTempDir, TMPDIR: relocateTempDir }
        : {}),
    },
    encoding: "utf8",
    timeout: 180000,
    maxBuffer: 16 * 1024 * 1024,
  });
  const wallMs = performance.now() - start;

  process.stdout.write(r.stdout || "");
  process.stderr.write(r.stderr || "");

  for (const name of fs.readdirSync(profileDir)) {
    const raw = fs.readFileSync(join(profileDir, name), "utf8").trim();
    if (!raw) continue;
    console.error(`[NATIVE-PROFILE] summary file=${name}`);
    console.error(raw);
  }
  console.error(
    `[NATIVE-PROFILE] wallMs=${wallMs.toFixed(1)} unchanged 180000ms process budget; status=${r.status} signal=${r.signal} error=${r.error?.code || "none"}`,
  );
  process.exitCode = r.status === 0 && !r.error && !r.signal ? 0 : 1;
} else {
  // Preload context (inside the --test child process). Instrument only.
  const callStats = new Map(); // "module#method" -> { calls, totalMs, maxMs }
  const phaseStats = new Map(); // phase label -> { calls, totalMs }
  const resolvedNativeModules = new Set();

  // Best-effort phase labels: if the test file (or anything it imports) sets
  // globalThis.__NATIVE_PROFILE_PHASE__ = "<label>" before an operation, every
  // native call recorded while that label is active is also attributed to
  // the phase bucket. This requires no test-assertion changes - it is a
  // plain global read, and its total absence (label never set) degrades to
  // all calls going in the "unphased" bucket, never a hard failure.
  const record = (key, ms) => {
    let entry = callStats.get(key);
    if (!entry) {
      entry = { calls: 0, totalMs: 0, maxMs: 0 };
      callStats.set(key, entry);
    }
    entry.calls += 1;
    entry.totalMs += ms;
    if (ms > entry.maxMs) entry.maxMs = ms;

    const label = globalThis.__NATIVE_PROFILE_PHASE__ || "unphased";
    let phaseEntry = phaseStats.get(label);
    if (!phaseEntry) {
      phaseEntry = { calls: 0, totalMs: 0 };
      phaseStats.set(label, phaseEntry);
    }
    phaseEntry.calls += 1;
    phaseEntry.totalMs += ms;
  };

  const wrappedModuleObjects = new WeakSet();
  function wrapExports(moduleId, exportsObj) {
    if (!exportsObj || typeof exportsObj !== "object") return exportsObj;
    if (wrappedModuleObjects.has(exportsObj)) return exportsObj;
    wrappedModuleObjects.add(exportsObj);
    const descriptorNames = Object.getOwnPropertyNames(exportsObj);
    for (const name of descriptorNames) {
      const descriptor = Object.getOwnPropertyDescriptor(exportsObj, name);
      if (!descriptor || typeof descriptor.value !== "function") continue;
      // Skip anything that looks like a class/constructor (native locks are
      // constructed with `new`; wrapping would break prototype chains for no
      // diagnostic benefit since this harness never constructs them).
      const fn = descriptor.value;
      const looksLikeClass =
        /^class\s/.test(Function.prototype.toString.call(fn)) ||
        (fn.prototype && Object.getOwnPropertyNames(fn.prototype).length > 1);
      if (looksLikeClass) continue;
      if (!descriptor.writable && !descriptor.configurable) continue;
      const key = `${moduleId}#${name}`;
      const wrapped = function (...args) {
        const t0 = performance.now();
        try {
          return fn.apply(this, args);
        } finally {
          record(key, performance.now() - t0);
        }
      };
      try {
        Object.defineProperty(exportsObj, name, { ...descriptor, value: wrapped });
      } catch {
        // Best-effort only: a non-configurable/non-writable export is left
        // unwrapped rather than failing the whole profiling pass.
      }
    }
    return exportsObj;
  }

  const originalLoad = Module._load;
  let sqliteWrapped = false;
  function wrapSqliteModule(mod) {
    if (sqliteWrapped) return mod;
    if (!mod || typeof mod !== "object" || typeof mod.DatabaseSync !== "function") return mod;
    sqliteWrapped = true;
    const OrigDatabaseSync = mod.DatabaseSync;

    function timeMethod(key, target, methodName) {
      const orig = target[methodName];
      if (typeof orig !== "function") return;
      target[methodName] = function (...callArgs) {
        const t0 = performance.now();
        try {
          return orig.apply(this, callArgs);
        } finally {
          record(key, performance.now() - t0);
        }
      };
    }

    function WrappedDatabaseSync(...ctorArgs) {
      const t0 = performance.now();
      const instance = new OrigDatabaseSync(...ctorArgs);
      record("DatabaseSync#constructor", performance.now() - t0);
      timeMethod("DatabaseSync#exec", instance, "exec");
      timeMethod("DatabaseSync#close", instance, "close");
      const origPrepare = instance.prepare;
      if (typeof origPrepare === "function") {
        instance.prepare = function (...prepareArgs) {
          const t1 = performance.now();
          const stmt = origPrepare.apply(this, prepareArgs);
          record("DatabaseSync#prepare", performance.now() - t1);
          timeMethod("StatementSync#run", stmt, "run");
          timeMethod("StatementSync#get", stmt, "get");
          timeMethod("StatementSync#all", stmt, "all");
          return stmt;
        };
      }
      return instance;
    }
    WrappedDatabaseSync.prototype = OrigDatabaseSync.prototype;

    const wrappedMod = Object.create(Object.getPrototypeOf(mod));
    for (const name of Object.getOwnPropertyNames(mod)) {
      const descriptor = Object.getOwnPropertyDescriptor(mod, name);
      if (name === "DatabaseSync") {
        try {
          Object.defineProperty(wrappedMod, name, { ...descriptor, value: WrappedDatabaseSync });
          continue;
        } catch {
          // Fall through to copy the original descriptor unwrapped below.
        }
      }
      try {
        Object.defineProperty(wrappedMod, name, descriptor);
      } catch {
        // Best-effort only.
      }
    }
    return wrappedMod;
  }

  Module._load = function (request, parent, isMain) {
    const result = originalLoad.call(this, request, parent, isMain);
    if (request === "node:sqlite") {
      return wrapSqliteModule(result);
    }
    const isNativeModule =
      typeof request === "string" &&
      (request.includes(NATIVE_SPECIFIER_PREFIX) || NATIVE_WORKSPACE_PATH_PATTERN.test(request));
    if (isNativeModule) {
      // Normalize to a short, stable label for logs: whatever comes at/after
      // the "@gsd/native" segment of the resolved path, with backslashes
      // folded to forward slashes (Windows paths) and the .js/.node
      // extension stripped so the same addon entry point reads identically
      // whether it was reached via a bare specifier (CJS require) or a
      // resolved absolute path (ESM-to-CJS interop).
      const normalized = request.replaceAll("\\", "/");
      const specifierIdx = normalized.indexOf(NATIVE_SPECIFIER_PREFIX);
      const workspaceIdx = normalized.indexOf("packages/native/dist/");
      const label =
        specifierIdx >= 0
          ? normalized.slice(specifierIdx).replace(/\.(js|node|cjs)$/, "")
          : workspaceIdx >= 0
            ? `@gsd/native/${normalized.slice(workspaceIdx + "packages/native/dist/".length).replace(/\/index\.(js|node|cjs)$/, "").replace(/\.(js|node|cjs)$/, "")}`
            : request;
      if (!resolvedNativeModules.has(label)) {
        resolvedNativeModules.add(label);
      }
      return wrapExports(label, result);
    }
    return result;
  };

  process.once("exit", () => {
    const lines = [`[NATIVE-PROFILE] pid=${process.pid} nativeModulesResolved=${JSON.stringify([...resolvedNativeModules])}`];
    for (const [key, entry] of [...callStats.entries()].sort((a, b) => b[1].totalMs - a[1].totalMs)) {
      lines.push(
        `[NATIVE-PROFILE] pid=${process.pid} call=${key} count=${entry.calls} wallMs=${entry.totalMs.toFixed(3)} maxMs=${entry.maxMs.toFixed(3)}`,
      );
    }
    if (callStats.size === 0) {
      lines.push(`[NATIVE-PROFILE] pid=${process.pid} nativeCalls=NONE (no @gsd/native export was invoked in this process)`);
    }
    lines.push(`[NATIVE-PROFILE] pid=${process.pid} sqliteWrapped=${sqliteWrapped}`);
    for (const [label, entry] of phaseStats.entries()) {
      lines.push(`[NATIVE-PROFILE] pid=${process.pid} phase=${label} calls=${entry.calls} wallMs=${entry.totalMs.toFixed(3)}`);
    }
    if (process.env.NATIVE_PROFILE_DIR) {
      try {
        fs.writeFileSync(join(process.env.NATIVE_PROFILE_DIR, `${process.pid}.log`), `${lines.join("\n")}\n`);
      } catch {
        // Diagnostic write failure must not affect test exit status.
      }
    }
    for (const line of lines) process.stderr.write(`${line}\n`);
  });
}
