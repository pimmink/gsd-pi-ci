// Test-first regression for the Windows MCP diagnostic blind-spot (D227 causal
// finding, windows-fix-20261009): the diagnostic harness used spawnSync(), which
// only returns stdout/stderr after the child process exits. Across the full
// 180000ms process budget that means the harness emits ZERO lines of test
// progress until the exact moment it is SIGTERMed by the timeout - every test
// that actually ran and passed before the kill is invisible, and "hung" vs.
// "slow but progressing" cannot be told apart from the transcript alone.
//
// This test exercises the harness's own runWithLiveOutput() helper (extracted
// so it is unit-testable without a real 180s node --test run) against a fake
// child that writes output in two bursts with a delay between them, then is
// killed by a short timeout. It asserts stdout is observable BEFORE the kill,
// not only after - the defect this fix targets - and that the exact
// production kill-signal/timeout/exit-code contract is unchanged.
import assert from "node:assert/strict";
import { test } from "node:test";
import mcpProfile from "./windows-mcp-profile.cjs";
const { runWithLiveOutput } = mcpProfile;

test("runWithLiveOutput surfaces stdout before the child is killed (not only after)", async () => {
  const seenBeforeKill = [];
  const result = await runWithLiveOutput(
    process.execPath,
    [
      "-e",
      // Write one line immediately, then hang well past the test's timeout.
      "process.stdout.write('first-line\\n'); setTimeout(() => {}, 5000);",
    ],
    {
      timeout: 1500,
      killSignal: "SIGTERM",
      onStdout: (chunk) => seenBeforeKill.push(chunk.toString()),
    },
  );
  assert.ok(
    seenBeforeKill.some((chunk) => chunk.includes("first-line")),
    "expected the first line to have been observed via onStdout before the kill, not only in the final buffered result",
  );
  assert.equal(result.signal, "SIGTERM", "kill-signal contract must be unchanged");
  assert.equal(result.status, null, "a killed child reports a null exit status, same as spawnSync");
  assert.ok(result.stdout.includes("first-line"), "the final aggregated stdout must still contain everything streamed");
});

test("runWithLiveOutput reports a clean exit identically to spawnSync when the child finishes in time", async () => {
  const result = await runWithLiveOutput(
    process.execPath,
    ["-e", "process.stdout.write('done\\n'); process.exit(0);"],
    { timeout: 5000, killSignal: "SIGTERM", onStdout: () => {} },
  );
  assert.equal(result.status, 0);
  assert.equal(result.signal, null);
  assert.equal(result.error, undefined);
  assert.ok(result.stdout.includes("done"));
});
