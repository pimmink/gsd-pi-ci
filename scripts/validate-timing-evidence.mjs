#!/usr/bin/env node

import { readFile } from "node:fs/promises";

export function validateTimingEvidence(evidence, expected) {
  const records = evidence.records;
  if (!evidence || evidence.schemaVersion !== 1 || !Array.isArray(records) || records.length === 0) throw new Error("timing evidence must use schema 1 and contain records");
  if (evidence.sourceSha !== expected.sourceSha || evidence.workflowSha !== expected.workflowSha || evidence.runId !== expected.runId) throw new Error("timing evidence top-level provenance mismatch");
  if (evidence.shardCount !== expected.shardCount || evidence.strategy !== "contiguous") throw new Error("timing evidence strategy or shard count mismatch");
  const paths = new Set();
  for (const record of records) {
    if (record.sourceSha !== expected.sourceSha || record.workflowSha !== expected.workflowSha || record.runId !== expected.runId || record.schemaVersion !== 1) throw new Error("timing record provenance mismatch");
    if (record.status !== "pass" || !Number.isFinite(record.durationMs) || record.durationMs < 0) throw new Error("timing record is incomplete or failed");
    if (typeof record.path !== "string" || !record.path.startsWith("dist-test/") || paths.has(record.path)) throw new Error("timing record path is invalid or duplicated");
    paths.add(record.path);
  }
  return { records: records.length, paths: paths.size };
}

const [file, sourceSha, workflowSha, runId, shardCount] = process.argv.slice(2);
if (file) {
  const evidence = JSON.parse(await readFile(file, "utf8"));
  console.log(JSON.stringify(validateTimingEvidence(evidence, { sourceSha, workflowSha, runId, shardCount: Number(shardCount) })));
}