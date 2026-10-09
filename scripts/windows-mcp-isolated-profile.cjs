// Diagnostic only: one unchanged file, live CPU/wall samples, original budget.
const fs = require("node:fs");
const cp = require("node:child_process");
const { resolve, join, basename } = require("node:path");
const { performance } = require("node:perf_hooks");
if (require.main === module) {
  const target = resolve(process.argv[2]);
  if (!target.endsWith(".test.js") || !fs.statSync(target).isFile()) throw Error("Expected one existing test file");
  const log = resolve(`isolated-${basename(target)}.ndjson`);
  fs.writeFileSync(log, "");
  const start = performance.now();
  const child = cp.spawn(process.execPath, ["--test", "--test-concurrency=1", "--test-reporter=tap", target], {
    env: { ...process.env, MCP_ISOLATED_CPU_LOG: log,
      NODE_OPTIONS: `${process.env.NODE_OPTIONS || ""} --require=${JSON.stringify(__filename.replaceAll("\\", "/"))}` },
    stdio: "inherit",
  });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; child.kill("SIGTERM"); }, 180000);
  let offset = 0;
  const flush = () => {
    const text = fs.readFileSync(log, "utf8");
    if (text.length > offset) process.stderr.write(text.slice(offset));
    offset = text.length;
  };
  const poll = setInterval(flush, 2000);
  child.once("close", (code, signal) => {
    clearTimeout(timeout); clearInterval(poll); flush();
    console.error(`[MCP-ISOLATED] wallMs=${(performance.now() - start).toFixed(1)} status=${code} signal=${signal} timedOut=${timedOut} budgetMs=180000`);
    process.exitCode = code === 0 && !signal && !timedOut ? 0 : 1;
  });
  child.once("error", (error) => { clearTimeout(timeout); clearInterval(poll); console.error(error); process.exitCode = 1; });
} else if (process.env.MCP_ISOLATED_CPU_LOG) {
  const start = performance.now();
  const cpu = process.cpuUsage();
  const sample = (phase) => {
    const usage = process.cpuUsage(cpu);
    fs.appendFileSync(process.env.MCP_ISOLATED_CPU_LOG, `${JSON.stringify({
      pid: process.pid, file: basename(process.argv[1] || "runner"), phase,
      wallMs: Number((performance.now() - start).toFixed(1)),
      cpuUserMs: usage.user / 1000, cpuSystemMs: usage.system / 1000,
    })}\n`);
  };
  sample("start");
  setInterval(() => sample("progress"), 5000).unref();
  process.once("exit", () => sample("exit"));
}
