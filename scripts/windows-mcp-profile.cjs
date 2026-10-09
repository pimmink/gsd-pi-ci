// Diagnostic-only instrumentation; never changes production registry semantics.
const cp = require("node:child_process");
const { syncBuiltinESMExports } = require("node:module");
const { resolve, join, basename } = require("node:path");
const { availableParallelism, cpus } = require("node:os");
const fs = require("node:fs");
const { performance } = require("node:perf_hooks");

// Visibility fix only (D227, windows-fix-20261009): the previous implementation used
// spawnSync(), which only returns stdout/stderr to this process after the
// child exits. Across the full 180000ms budget that means every line of TAP
// test progress was invisible until the exact instant the timeout fired and
// killed the child - a diagnostic that cannot tell "hung" apart from "slow
// but steadily progressing" from the transcript, which is the actual
// diagnostic question this harness exists to answer. runWithLiveOutput()
// replaces spawnSync with spawn() plus a manual timeout/kill that mirrors
// spawnSync's exact result contract (status/signal/stdout/stderr/error), so
// output streams to the caller as it is produced while every downstream
// consumer (the budget check, the exit-code mapping) is unchanged.
function runWithLiveOutput(command, args, { timeout, killSignal = "SIGTERM", env, onStdout, onStderr } = {}) {
  return new Promise((resolvePromise) => {
    const child = cp.spawn(command, args, { env });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let settled = false;
    const timer =
      timeout > 0
        ? setTimeout(() => {
            timedOut = true;
            child.kill(killSignal);
          }, timeout)
        : null;
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      onStdout?.(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      onStderr?.(chunk);
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolvePromise({ status: null, signal: null, stdout, stderr, error });
    });
    child.on("close", (status, signal) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolvePromise({
        status,
        signal,
        stdout,
        stderr,
        error: timedOut ? Object.assign(new Error("ETIMEDOUT"), { code: "ETIMEDOUT" }) : undefined,
      });
    });
  });
}

// The 4-CPU Windows runner defaults to 3 workers. The workflow file took
// >180s under concurrent heavy workers, but 71.7s alone (20.3s CPU).
// Two workers also exhausted the budget: use measured exclusive execution.
// Preserve process isolation, every file/assertion, and the original budget.
const TEST_CONCURRENCY = 1;
const HEAVY_FILES = ["state-md-render.test.js", "workflow-tools.test.js", "workflow-tools-parity.test.js"];
function scheduleTargets(targets) {
  const rank = (target) => {
    const index = HEAVY_FILES.indexOf(basename(target));
    return index < 0 ? HEAVY_FILES.length : index;
  };
  return [...targets].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

async function main() {
  const target = resolve(process.argv[2]);
  const targets = fs.statSync(target).isDirectory()
    ? fs
        .readdirSync(target, { recursive: true })
        .filter((name) => name.endsWith(".test.js"))
        .sort()
        .map((name) => join(target, name))
    : [target];
  if (!targets.length) throw new Error("No MCP test files found");
  const scheduled = scheduleTargets(targets);
  console.error(`[MCP-DIAG] selected ${scheduled.length} test files; only this MCP package`);
  console.error(`[MCP-DIAG] cpuCount=${cpus().length} availableParallelism=${availableParallelism()} nodeDefaultConcurrency=${Math.max(1, availableParallelism() - 1)} chosenConcurrency=${TEST_CONCURRENCY}`);
  console.error(`[MCP-DIAG] schedule=${scheduled.map((file) => basename(file)).join(",")}`);
  const profileDir = resolve("mcp-diagnostic-profiles");
  fs.mkdirSync(profileDir, { recursive: true });
  const start = performance.now();
  const r = await runWithLiveOutput(process.execPath, [
    join(__dirname, "windows-mcp-runner.cjs"), ...scheduled,
  ], {
    env: process.argv.includes("--no-profile")
      ? process.env
      : {
          ...process.env,
          MCP_PROFILE_DIR: profileDir,
          NODE_OPTIONS: `${process.env.NODE_OPTIONS || ""} --require=${JSON.stringify(__filename.replaceAll("\\", "/"))}`,
        },
    timeout: 180000,
    killSignal: "SIGTERM",
    // Stream as it arrives instead of buffering silently for the full budget.
    onStdout: (chunk) => process.stdout.write(chunk),
    onStderr: (chunk) => process.stderr.write(chunk),
  });
  for (const name of fs.readdirSync(profileDir)) {
    const lines = fs.readFileSync(join(profileDir, name), "utf8").trim().split("\n");
    console.error(`[MCP-DIAG] profile=${name} events=${lines.length}`);
    console.error(lines.slice(-10).join("\n"));
  }
  console.error(
    `[MCP-DIAG] unchanged 180000ms process budget; wallMs=${(performance.now() - start).toFixed(1)} status=${r.status} signal=${r.signal} error=${r.error?.code || "none"}`,
  );
  process.exitCode = r.status === 0 && !r.error && !r.signal ? 0 : 1;
}

if (require.main === module) {
  main();
} else {
  const original = cp.execFileSync;
  const emit = (line) => {
    console.error(line);
    if (process.env.MCP_PROFILE_DIR)
      fs.appendFileSync(join(process.env.MCP_PROFILE_DIR, `${process.pid}.log`), `${line}\n`);
  };
  let calls = 0;
  let totalMs = 0;
  process.on("uncaughtExceptionMonitor", (error) =>
    emit(
      `[MCP-DIAG] uncaught name=${error.name} code=${error.code || "none"} message=${String(error.message).replaceAll("\n", " ")}`,
    ),
  );
  const originalSpawn = cp.spawn;
  let writer = 0;
  cp.spawn = function (command, args, options) {
    // Diagnostic-only stderr routing for the eval writer otherwise hidden by
    // stdio:ignore. Never change its code, argv, cwd, env or exit handling.
    if (
      command !== process.execPath ||
      !args?.includes("--eval") ||
      !Array.isArray(options?.stdio) ||
      options.stdio[2] !== "ignore" ||
      !process.env.MCP_PROFILE_DIR
    )
      return originalSpawn.call(this, command, args, options);
    const fd = fs.openSync(
      join(process.env.MCP_PROFILE_DIR, `writer-${process.pid}-${++writer}.stderr`),
      "a",
    );
    try {
      const child = originalSpawn.call(this, command, args, {
        ...options,
        stdio: [options.stdio[0], options.stdio[1], fd],
      });
      child.once("close", () => fs.closeSync(fd));
      return child;
    } catch (error) {
      fs.closeSync(fd);
      throw error;
    }
  };
  cp.execFileSync = function (command, args, ...rest) {
    if (!String(command).toLowerCase().endsWith("powershell.exe"))
      return original.call(this, command, args, ...rest);
    const script = Array.isArray(args) ? args.join(" ") : "";
    const category = script.includes("CreationDate")
      ? "start-time"
      : script.includes("CommandLine")
        ? "command-line"
        : "other-process-query";
    const seq = ++calls;
    const start = performance.now();
    // Only counts/categories/timing: never command lines, return values or env.
    emit(`[MCP-DIAG] pid=${process.pid} query=${seq} category=${category} begin`);
    try {
      return original.call(this, command, args, ...rest);
    } finally {
      const ms = performance.now() - start;
      totalMs += ms;
      emit(
        `[MCP-DIAG] pid=${process.pid} query=${seq} category=${category} durationMs=${ms.toFixed(1)} cumulativeMs=${totalMs.toFixed(1)}`,
      );
    }
  };
  syncBuiltinESMExports();
}

module.exports = { runWithLiveOutput, scheduleTargets, TEST_CONCURRENCY };
