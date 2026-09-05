#!/usr/bin/env node

import { readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export async function assembleTimingEvidence({ timingsDir, sourceSha, workflowSha, runId, shardCount, manifestSha256 }) {
  const files = (await readdir(timingsDir)).filter((file) => file.endsWith(".json")).sort();
  if (files.length !== Number(shardCount)) throw new Error(`expected ${shardCount} timing files, found ${files.length}`);
  const records = (await Promise.all(files.map(async (file) => JSON.parse(await readFile(join(timingsDir, file), "utf8"))))).flat();
  const first = records[0] ?? {};
  return {
    schemaVersion: 1,
    sourceSha,
    workflowSha,
    manifestSha256,
    lockfileSha256: first.lockfileSha256,
    node: first.node,
    runnerImage: first.runnerImage,
    shardCount: Number(shardCount),
    runId,
    strategy: "contiguous",
    records,
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [timingsDir, outputFile, sourceSha, workflowSha, runId, shardCount, manifestSha256] = process.argv.slice(2);
  if (!timingsDir || !outputFile || !sourceSha || !workflowSha || !runId || !shardCount || !manifestSha256) {
    throw new Error("usage: assemble-timing-evidence.mjs <timings-dir> <output-file> <source-sha> <workflow-sha> <run-id> <shard-count> <manifest-sha256>");
  }
  const evidence = await assembleTimingEvidence({ timingsDir, sourceSha, workflowSha, runId, shardCount, manifestSha256 });
  await writeFile(outputFile, `${JSON.stringify(evidence, null, 2)}\n`);
}