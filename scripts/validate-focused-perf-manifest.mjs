#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, basename } from "node:path";

const readJson = (file) => JSON.parse(readFileSync(file, "utf8"));
const readManifest = (file) => readFileSync(file, "utf8").split(/\r?\n/).filter(Boolean);
const escapeRegex = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function expand(globs) {
  const files = new Set();
  const matches = [];
  for (const pattern of globs) {
    const directory = dirname(pattern);
    const filePattern = basename(pattern);
    const regex = new RegExp(`^${filePattern.split("*").map(escapeRegex).join(".*")}$`);
    let directoryEntries;
    try {
      directoryEntries = readdirSync(directory);
    } catch (error) {
      throw new Error(`missing compiled glob directory: ${directory}: ${error.message}`);
    }
    for (const entry of directoryEntries) {
      if (regex.test(entry)) matches.push(join(directory, entry));
    }
  }
  if (matches.length !== new Set(matches).size) throw new Error("source glob expansion contains duplicate compiled tests");
  for (const file of matches) files.add(file);
  return [...files].sort();
}

function assertManifest(label, actual, expected) {
  const sorted = [...actual].sort();
  if (actual.length !== new Set(actual).size || actual.some((value, index) => value !== sorted[index])) {
    throw new Error(`${label} is not sorted and unique`);
  }
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label} differs from its checked-out package.json glob expansion`);
  }
}

const ordinaryGlobs = readJson("test-globs.json");
const performanceGlobs = readJson("perf-test-globs.json");
const ordinary = readManifest("test-manifest.txt");
const performance = readManifest("perf-test-manifest.txt");

if (performanceGlobs.length && !performanceGlobs.every((pattern) => /^dist-test\/src\/tests\/performance\/\*\.test\.(?:js|mjs)$/.test(pattern))) {
  throw new Error("performance globs must be compiled src/tests/performance test paths");
}

assertManifest("ordinary manifest", ordinary, expand(ordinaryGlobs));
assertManifest("performance manifest", performance, expand(performanceGlobs));

const overlap = ordinary.filter((file) => performance.includes(file));
if (overlap.length > 0) throw new Error(`ordinary/performance manifest overlap: ${overlap.join(", ")}`);
if (!performance.every((file) => file.startsWith("dist-test/src/tests/performance/"))) {
  throw new Error("performance manifest contains a non-performance compiled path");
}

const actualUnion = [...ordinary, ...performance].sort();
const expectedUnion = [...expand(ordinaryGlobs), ...expand(performanceGlobs)].sort();
if (actualUnion.length !== new Set(actualUnion).size) throw new Error("ordinary + performance union contains duplicates");
if (JSON.stringify(actualUnion) !== JSON.stringify(expectedUnion)) {
  throw new Error("ordinary + performance manifest union omits or duplicates compiled tests");
}

for (const [manifest, receipt] of [["test-manifest.txt", "test-manifest.sha256"], ["perf-test-manifest.txt", "perf-test-manifest.sha256"]]) {
  const expectedHash = readFileSync(receipt, "utf8").trim().split(/\s+/)[0];
  const actualHash = createHash("sha256").update(readFileSync(manifest)).digest("hex");
  if (expectedHash !== actualHash) throw new Error(`${receipt} does not match ${manifest}`);
}

console.log(`Focused manifest coverage passed: ${ordinary.length} ordinary + ${performance.length} performance = ${actualUnion.length} unique compiled tests.`);
