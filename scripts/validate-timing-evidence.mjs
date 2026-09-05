#!/usr/bin/env node

import { readFile } from "node:fs/promises";

export function validateTimingEvidence(evidence, expected, manifestPaths) {
  const records = evidence?.records;
  if (!evidence || evidence.schemaVersion !== 1 || !Array.isArray(records) || records.length === 0) throw new Error("timing evidence must use schema 1 and contain records");
  if (evidence.sourceSha !== expected.sourceSha || evidence.workflowSha !== expected.workflowSha || evidence.runId !== expected.runId) throw new Error("timing evidence top-level provenance mismatch");
  if (evidence.shardCount !== expected.shardCount || evidence.strategy !== "contiguous" || evidence.manifestSha256 !== expected.manifestSha256) throw new Error("timing evidence strategy, shard count, or manifest mismatch");
  const paths = new Set();
  const environment = { lockfileSha256: records[0]?.lockfileSha256, node: records[0]?.node, runnerImage: records[0]?.runnerImage };
  if (Object.values(environment).some((value) => typeof value !== "string" || value.length === 0 || value.includes("unknown"))) throw new Error("timing evidence environment provenance is incomplete");
  if (evidence.lockfileSha256 !== environment.lockfileSha256 || evidence.node !== environment.node || evidence.runnerImage !== environment.runnerImage) throw new Error("timing evidence environment provenance mismatch");
  for (const record of records) {
    if (record.sourceSha !== expected.sourceSha || record.workflowSha !== expected.workflowSha || record.runId !== expected.runId || record.schemaVersion !== 1 || record.manifestSha256 !== expected.manifestSha256 || record.shardCount !== expected.shardCount || record.lockfileSha256 !== environment.lockfileSha256 || record.node !== environment.node || record.runnerImage !== environment.runnerImage) throw new Error("timing record provenance mismatch");
    if (record.status !== "pass" || !Number.isFinite(record.durationMs) || record.durationMs < 0) throw new Error("timing record is incomplete or failed");
    if (typeof record.path !== "string" || !record.path.startsWith("dist-test/") || paths.has(record.path)) throw new Error("timing record path is invalid or duplicated");
    paths.add(record.path);
  }
  if (manifestPaths) {
    if (paths.size !== manifestPaths.size || [...manifestPaths].some((path) => !paths.has(path))) throw new Error("timing evidence does not exactly cover the canonical manifest");
  }
  return { records: records.length, paths: paths.size };
}

const [file, sourceSha, workflowSha, runId, shardCount, manifestSha256, manifestFile] = process.argv.slice(2);
if (file) {
  const evidence = JSON.parse(await readFile(file, "utf8"));
  const manifestPaths = manifestFile ? new Set((await readFile(manifestFile, "utf8")).split("\n").filter(Boolean)) : undefined;
  console.log(JSON.stringify(validateTimingEvidence(evidence, { sourceSha, workflowSha, runId, shardCount: Number(shardCount), manifestSha256 }, manifestPaths)));
}