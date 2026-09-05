import test from "node:test";
import assert from "node:assert/strict";
import { validateTimingEvidence } from "../validate-timing-evidence.mjs";

const expected = { sourceSha: "a".repeat(40), workflowSha: "b".repeat(40), runId: "42", shardCount: 4 };
const record = (overrides = {}) => ({ path: "dist-test/src/example.test.js", durationMs: 12, status: "pass", sourceSha: expected.sourceSha, workflowSha: expected.workflowSha, runId: expected.runId, schemaVersion: 1, ...overrides });

test("accepts complete contiguous timing evidence", () => {
  assert.deepEqual(validateTimingEvidence({ schemaVersion: 1, ...expected, strategy: "contiguous", records: [record()] }, expected), { records: 1, paths: 1 });
});

for (const [name, overrides] of [["stale SHA", { sourceSha: "c".repeat(40) }], ["failed measurement", { status: "fail" }], ["unsafe path", { path: "/tmp/test.js" }]]) {
  test(`rejects ${name}`, () => assert.throws(() => validateTimingEvidence({ schemaVersion: 1, ...expected, strategy: "contiguous", records: [record(overrides)] }, expected)));
}