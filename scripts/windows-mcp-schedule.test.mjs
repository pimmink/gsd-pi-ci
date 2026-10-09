import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import runner from "./windows-mcp-profile.cjs";

const { runWithLiveOutput, scheduleTargets, TEST_CONCURRENCY } = runner;
test("schedule launches heavy files first without dropping/duplicating any of 24 files", () => {
  const files = Array.from({ length: 21 }, (_, i) => `small-${i}.test.js`).concat([
    "state-md-render.test.js", "workflow-tools.test.js", "workflow-tools-parity.test.js",
  ]);
  const original = [...files];
  const scheduled = scheduleTargets(files);
  assert.equal(TEST_CONCURRENCY, 2);
  assert.deepEqual(scheduled.slice(0, 3), files.slice(-3));
  assert.equal(scheduled.length, 24);
  assert.deepEqual([...scheduled].sort(), [...files].sort());
  assert.deepEqual(files, original);
});

test("Node 24 programmatic runner preserves schedule, isolation, full TAP and failure exit", async () => {
  const dir = mkdtempSync(join(tmpdir(), "mcp-schedule-"));
  try {
    const files = ["z-heavy.test.js", "a-light.test.js"].map((name) => join(dir, name));
    for (const file of files) writeFileSync(file, `const {test}=require('node:test'); test(${JSON.stringify(file)},()=>{});\n`);
    const env = { ...process.env };
    delete env.NODE_TEST_CONTEXT;
    const args = [join(import.meta.dirname, "windows-mcp-runner.cjs"), ...files];
    const result = await runWithLiveOutput(process.execPath, args, { timeout: 5000, env });
    assert.equal(result.status, 0);
    const summaries = result.stderr.split("\n").filter((line) => line.startsWith("[MCP-FILE] ")).map((line) => JSON.parse(line.slice(11)));
    assert.deepEqual(summaries.map((s) => s.file).sort(), ["ALL", "a-light.test.js", "z-heavy.test.js"]);
    assert.ok(result.stdout.indexOf(files[0]) < result.stdout.indexOf(files[1]), "explicit schedule must survive Node's discovery sorting");
    assert.equal(summaries.at(-1).counts.tests, 2);
    assert.match(result.stdout, /# tests 2\n# suites 0\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 0/);
    writeFileSync(files[1], "const {test}=require('node:test'); test('real failure',()=>{throw Error('intentional');});\n");
    const failed = await runWithLiveOutput(process.execPath, args, { timeout: 5000, env });
    assert.equal(failed.status, 1);
    assert.match(failed.stdout, /# tests 2\n# suites 0\n# pass 1\n# fail 1\n# cancelled 0\n# skipped 0/);
    assert.match(failed.stderr, /"success":false/);
    const eventsFile = join(dir, "worker-events.ndjson");
    writeFileSync(eventsFile, "");
    const boundedFiles = ["one.test.js", "two.test.js", "three.test.js"].map((name) => join(dir, name));
    for (const file of boundedFiles) writeFileSync(file, `const {test}=require('node:test'); const fs=require('node:fs'); const log=${JSON.stringify(eventsFile)}; test('isolated bounded worker',async()=>{fs.appendFileSync(log,JSON.stringify({pid:process.pid,delta:1})+'\\n');await new Promise(r=>setTimeout(r,500));fs.appendFileSync(log,JSON.stringify({pid:process.pid,delta:-1})+'\\n');});\n`);
    const bounded = await runWithLiveOutput(process.execPath, [join(import.meta.dirname, "windows-mcp-runner.cjs"), ...boundedFiles], { timeout: 10000, env });
    assert.equal(bounded.status, 0);
    const events = readFileSync(eventsFile, "utf8").trim().split("\n").map((line) => JSON.parse(line));
    let active = 0; let maximum = 0;
    for (const event of events) { active += event.delta; maximum = Math.max(maximum, active); assert.notEqual(event.pid, process.pid); }
    assert.equal(maximum, 2, "exactly two process-isolated workers, never three");
    assert.equal(active, 0);
    assert.equal(new Set(events.map((event) => event.pid)).size, 3);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
