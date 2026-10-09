// CLI discovery sorts explicit file arguments; the public run({files}) API
// preserves this schedule. Workers remain process-isolated and uninstrumented.
const { run } = require("node:test");
const { tap } = require("node:test/reporters");
const { performance } = require("node:perf_hooks");
const { basename } = require("node:path");
const start = performance.now();
const tests = run({ files: process.argv.slice(2), concurrency: 2 });
tests.on("test:summary", (data) => {
  console.error(`[MCP-FILE] ${JSON.stringify({
    file: data.file ? basename(data.file) : "ALL",
    elapsedMs: Number((performance.now() - start).toFixed(1)),
    durationMs: data.duration_ms,
    success: data.success,
    counts: data.counts,
  })}`);
  if (!data.success) process.exitCode = 1;
});
tests.on("error", (error) => { console.error(error); process.exitCode = 1; });
tests.compose(tap).pipe(process.stdout);
