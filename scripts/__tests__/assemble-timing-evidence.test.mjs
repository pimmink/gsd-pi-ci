import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assembleTimingEvidence } from "../assemble-timing-evidence.mjs";

const provenance = {
  sourceSha: "a".repeat(40),
  workflowSha: "b".repeat(40),
  manifestSha256: "c".repeat(64),
  lockfileSha256: "d".repeat(64),
  node: "v24.20.0",
  runnerImage: "ubuntu24:20260901.1",
  shardCount: 2,
  runId: "42",
};

test("assembles records from every shard in deterministic filename order", async () => {
  const directory = await mkdtemp(join(tmpdir(), "timing-evidence-"));
  try {
    await writeFile(join(directory, "timings-shard-2.json"), JSON.stringify([{ ...provenance, shard: 2, path: "dist-test/src/two.test.js", durationMs: 2, status: "pass", schemaVersion: 1 }]));
    await writeFile(join(directory, "timings-shard-1.json"), JSON.stringify([{ ...provenance, shard: 1, path: "dist-test/src/one.test.js", durationMs: 1, status: "pass", schemaVersion: 1 }]));
    const evidence = await assembleTimingEvidence({ timingsDir: directory, ...provenance });
    assert.deepEqual(evidence.records.map(({ path }) => path), ["dist-test/src/one.test.js", "dist-test/src/two.test.js"]);
    assert.equal(evidence.strategy, "contiguous");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("rejects a missing shard artifact", async () => {
  const directory = await mkdtemp(join(tmpdir(), "timing-evidence-"));
  try {
    await writeFile(join(directory, "timings-shard-1.json"), "[]");
    await assert.rejects(() => assembleTimingEvidence({ timingsDir: directory, ...provenance }));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});