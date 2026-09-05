import test from "node:test";
import assert from "node:assert/strict";
import { validateTimingEvidence } from "../validate-timing-evidence.mjs";

const expected = { sourceSha: "a".repeat(40), workflowSha: "b".repeat(40), runId: "42", shardCount: 4, manifestSha256: "c".repeat(64) };
const environment = { lockfileSha256: "d".repeat(64), node: "v24.20.0", runnerImage: "ubuntu24:20260901.1" };
const record = (overrides = {}) => ({ path: "dist-test/src/example.test.js", durationMs: 12, status: "pass", ...expected, ...environment, schemaVersion: 1, shard: 1, ...overrides });
const evidence = (records = [record()], overrides = {}) => ({ schemaVersion: 1, ...expected, ...environment, strategy: "contiguous", records, ...overrides });

test("accepts complete contiguous timing evidence", () => {
  assert.deepEqual(validateTimingEvidence(evidence(), expected), { records: 1, paths: 1 });
});

for (const [name, overrides] of [["top-level schema", { schemaVersion: 2 }], ["top-level source SHA", { sourceSha: "e".repeat(40) }], ["top-level workflow SHA", { workflowSha: "e".repeat(40) }], ["top-level run ID", { runId: "43" }], ["top-level shard count", { shardCount: 3 }], ["top-level strategy", { strategy: "historical-greedy" }], ["top-level manifest", { manifestSha256: "e".repeat(64) }], ["empty records", { records: [] }], ["incomplete environment", { node: "" }], ["unknown runner", { runnerImage: "unknown:unknown" }]]) {
  test(`rejects ${name}`, () => assert.throws(() => validateTimingEvidence(evidence([record()], overrides), expected)));
}

for (const [name, overrides] of [["stale SHA", { sourceSha: "e".repeat(40) }], ["workflow drift", { workflowSha: "e".repeat(40) }], ["run drift", { runId: "43" }], ["schema drift", { schemaVersion: 2 }], ["shard-count drift", { shardCount: 3 }], ["failed measurement", { status: "fail" }], ["unsafe path", { path: "/tmp/test.js" }], ["manifest mismatch", { manifestSha256: "e".repeat(64) }], ["lockfile drift", { lockfileSha256: "e".repeat(64) }], ["missing duration", { durationMs: Number.NaN }]]) {
  test(`rejects ${name}`, () => assert.throws(() => validateTimingEvidence(evidence([record(overrides)]), expected)));
}

test("rejects duplicate paths", () => {
  assert.throws(() => validateTimingEvidence(evidence([record(), record()]), expected));
});

test("rejects an out-of-manifest path when the caller supplies the canonical manifest", () => {
  assert.throws(() => validateTimingEvidence(evidence([record({ path: "dist-test/src/other.test.js" })]), expected, new Set([record().path])));
});

test("rejects an under-covered canonical manifest", () => {
  assert.throws(() => validateTimingEvidence(evidence(), expected, new Set([record().path, "dist-test/src/missing.test.js"])));
});

test("rejects cross-shard environment drift", () => {
  assert.throws(() => validateTimingEvidence(evidence([record(), record({ path: "dist-test/src/other.test.js", node: "v25.0.0" })]), expected, new Set([record().path, "dist-test/src/other.test.js"])));
});

test("accepts multi-record exact manifest coverage", () => {
  const records = [record(), record({ path: "dist-test/src/other.test.js" })];
  assert.deepEqual(validateTimingEvidence(evidence(records), expected, new Set(records.map(({ path }) => path))), { records: 2, paths: 2 });
});