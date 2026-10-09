import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workflow = readFileSync(new URL("../.github/workflows/remote-pr-verification-sharded.yml", import.meta.url), "utf8");
const manifestGuard = readFileSync(new URL("./validate-focused-perf-manifest.mjs", import.meta.url), "utf8");
const dispatchHelper = readFileSync(new URL("./remote-verify.sh", import.meta.url), "utf8");

const position = (text) => {
  const index = workflow.indexOf(text);
  assert.notEqual(index, -1, `workflow is missing: ${text}`);
  return index;
};

test("focused performance profile is explicit and disables unrelated jobs", () => {
  assert.match(workflow, /focused_perf_only:\n\s+description:.*serialized performance gate/);
  assert.match(workflow, /windows-mcp:\n\s+if: \$\{\{ inputs\.windows_mcp_only && !inputs\.focused_perf_only \}\}/);
  assert.match(workflow, /build:\n\s+if: \$\{\{ !inputs\.windows_mcp_only \|\| inputs\.focused_perf_only \}\}/);
  assert.match(workflow, /test-shard:\n[\s\S]*?if: \$\{\{ !inputs\.focused_perf_only \}\}/);
  assert.match(workflow, /timing-shard:\n[\s\S]*?if: \$\{\{ inputs\.collect_timings && !inputs\.focused_perf_only \}\}/);
  assert.match(workflow, /lifecycle-gate:\n[\s\S]*?if: \$\{\{ !inputs\.focused_perf_only \}\}/);
  assert.match(workflow, /always\(\) && !inputs\.windows_mcp_only && !inputs\.focused_perf_only/);
  assert.match(workflow, /registry_diagnostics && !inputs\.focused_perf_only/);
  assert.match(workflow, /performance_diagnostics && !inputs\.focused_perf_only/);
  assert.match(workflow, /Compute shard matrix from shard_count input\n\s+if: \$\{\{ !inputs\.focused_perf_only \}\}/);
});

test("focused gate uses the agreed source and compiled performance contract", () => {
  assert.match(workflow, /src\/tests\/performance\/\*\.test\.ts/);
  assert.ok(workflow.includes("dist-test/src/tests/performance/*.test.js or *.test.mjs"), "workflow is missing the compiled performance path contract");
  assert.match(workflow, /test:unit:compiled:perf/);
  assert.match(workflow, /Run declared serialized performance gate[\s\S]*?pnpm run test:unit:compiled:perf/);
  assert.match(workflow, /node scripts\/validate-focused-perf-manifest\.mjs/);
  assert.match(workflow, /perf-test-manifest\.sha256/);
});

test("focused gate is ordered after the complete canonical build and manifest guards", () => {
  const compile = position("- name: Compile test artifacts");
  const nativeMirror = position("- name: Mirror native addon into dist-test");
  const provenance = position("- name: Record focused performance provenance and native hash receipt");
  const manifest = position("- name: Expand declared performance manifest");
  const gate = position("- name: Run declared serialized performance gate");
  assert.ok(compile < nativeMirror, "test compilation must precede native mirror");
  assert.ok(nativeMirror < provenance, "native readback/hash receipt must precede the focused gate");
  assert.ok(provenance < manifest, "toolchain/hash provenance must precede manifest validation");
  assert.ok(manifest < gate, "both manifest guards must pass before the performance gate");
});

test("manifest guard proves disjoint exact union and hash receipts", () => {
  assert.match(manifestGuard, /ordinary manifest/);
  assert.match(manifestGuard, /performance manifest/);
  assert.match(manifestGuard, /ordinary\/performance manifest overlap/);
  assert.match(manifestGuard, /ordinary \+ performance union contains duplicates/);
  assert.match(manifestGuard, /manifest union omits or duplicates compiled tests/);
  assert.match(manifestGuard, /sha256/);
});

test("dispatch helper exposes the focused profile without changing other modes", () => {
  assert.match(dispatchHelper, /--focused-perf-only/);
  assert.match(dispatchHelper, /FOCUSED_PERF_ONLY=false/);
  assert.match(dispatchHelper, /focused_perf_only=true/);
  assert.match(dispatchHelper, /only valid with --mode sharded/);
});

test("focused profile records exact source and toolchain provenance", () => {
  assert.match(workflow, /EXPECTED_SHA: \$\{\{ inputs\.expected_sha \}\}/);
  assert.match(workflow, /actual_sha="\$\(git rev-parse HEAD\)"/);
  assert.match(workflow, /native\/addon\/\*\.node/);
  assert.match(workflow, /dist-test\/native\/addon\/\*\.node/);
  assert.match(workflow, /node --version/);
  assert.match(workflow, /pnpm --version/);
  assert.match(workflow, /rustc --version/);
  assert.match(workflow, /cargo --version/);
  assert.match(workflow, /sha256sum/);
});
