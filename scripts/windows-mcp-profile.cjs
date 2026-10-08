// Diagnostic-only instrumentation; never changes production registry semantics.
const cp = require("node:child_process");
const { syncBuiltinESMExports } = require("node:module");
const { resolve, join } = require("node:path");
const fs = require("node:fs");
const { performance } = require("node:perf_hooks");
if (require.main === module) {
  const target = resolve(process.argv[2]);
  const profileDir = resolve("mcp-diagnostic-profiles");
  fs.mkdirSync(profileDir, { recursive: true });
  const r = cp.spawnSync(process.execPath, ["--test", "--test-reporter=tap", target], {
    env: {
      ...process.env,
      MCP_PROFILE_DIR: profileDir,
      NODE_OPTIONS: `${process.env.NODE_OPTIONS || ""} --require=${JSON.stringify(__filename.replaceAll("\\", "/"))}`,
    },
    encoding: "utf8",
    timeout: 180000,
    maxBuffer: 16 * 1024 * 1024,
  });
  process.stdout.write(r.stdout || "");
  process.stderr.write(r.stderr || "");
  for (const name of fs.readdirSync(profileDir)) {
    const lines = fs.readFileSync(join(profileDir, name), "utf8").trim().split("\n");
    console.error(`[MCP-DIAG] profile=${name} events=${lines.length}`);
    console.error(lines.slice(-10).join("\n"));
  }
  console.error(
    `[MCP-DIAG] unchanged 180000ms process budget; status=${r.status} signal=${r.signal} error=${r.error?.code || "none"}`,
  );
  process.exitCode = r.status === 0 && !r.error && !r.signal ? 0 : 1;
} else {
  const original = cp.execFileSync;
  const emit = (line) => {
    console.error(line);
    if (process.env.MCP_PROFILE_DIR)
      fs.appendFileSync(join(process.env.MCP_PROFILE_DIR, `${process.pid}.log`), `${line}\n`);
  };
  let calls = 0;
  let totalMs = 0;
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
