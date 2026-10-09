import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
  assert.match(workflow, /node "\$RUNNER_TEMP\/gsd-perf-harness\/scripts\/validate-focused-perf-manifest\.mjs"/);
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

test("validator is staged at the pinned harness SHA and executes from the separate source checkout", () => {
  assert.match(workflow, /git -C "\$harness_dir" fetch --depth=1 origin "\$HARNESS_SHA"/);
  assert.match(workflow, /harness_dir="\$RUNNER_TEMP\/gsd-perf-harness"/);
  assert.ok(position("Stage exact harness validator outside source checkout") < position("Expand declared performance manifest"));
  const root = mkdtempSync(join(tmpdir(), "gsd-perf-layout-"));
  try {
    const source = join(root, "source");
    const runnerTemp = join(root, "runner-temp");
    const harness = join(runnerTemp, "gsd-perf-harness", "scripts");
    mkdirSync(harness, { recursive: true });
    mkdirSync(join(source, "dist-test/src/tests/performance"), { recursive: true });
    copyFileSync(new URL("./validate-focused-perf-manifest.mjs", import.meta.url), join(harness, "validate-focused-perf-manifest.mjs"));
    const ordinary = "dist-test/src/tests/plain.test.js";
    const performance = "dist-test/src/tests/performance/workflow-performance-baseline.test.js";
    writeFileSync(join(source, ordinary), "");
    writeFileSync(join(source, performance), "");
    writeFileSync(join(source, "dist-test/src/tests/performance/not-a-test.testXjs"), "");
    writeFileSync(join(source, "test-globs.json"), JSON.stringify(["dist-test/src/tests/*.test.js"]));
    writeFileSync(join(source, "perf-test-globs.json"), JSON.stringify(["dist-test/src/tests/performance/*.test.js"]));
    writeFileSync(join(source, "test-manifest.txt"), `${ordinary}\n`);
    const producerStep = workflow.split("- name: Expand declared performance manifest")[1];
    const producer = producerStep.match(/node - <<'NODE'\n([\s\S]*?)\n          NODE/)?.[1];
    assert.ok(producer);
    const produced = spawnSync(process.execPath, ["-e", producer], { cwd: source, encoding: "utf8" });
    assert.equal(produced.status, 0, produced.stderr);
    assert.equal(readFileSync(join(source, "perf-test-manifest.txt"), "utf8"), `${performance}\n`, "actual workflow producer must emit real newlines, not escaped literals");
    for (const file of ["test-manifest.txt", "perf-test-manifest.txt"]) {
      const digest = createHash("sha256").update(readFileSync(join(source, file))).digest("hex");
      writeFileSync(join(source, `${file.replace(/\.txt$/, "")}.sha256`), `${digest}  ${file}\n`);
    }
    const productionPath = workflow.match(/^\s+node "(\$RUNNER_TEMP\/gsd-perf-harness\/scripts\/validate-focused-perf-manifest\.mjs)"$/m)?.[1];
    assert.ok(productionPath);
    const run = () => spawnSync(process.execPath, [productionPath.replace("$RUNNER_TEMP", runnerTemp)], { cwd: source, encoding: "utf8" });
    const good = run();
    assert.equal(good.status, 0, good.stderr);
    writeFileSync(join(source, "perf-test-manifest.txt"), "");
    const missing = run();
    assert.notEqual(missing.status, 0);
    assert.match(missing.stderr, /performance manifest differs/);
    writeFileSync(join(source, "perf-test-manifest.txt"), `${performance}\n${performance}\n`);
    const duplicate = run();
    assert.notEqual(duplicate.status, 0);
    assert.match(duplicate.stderr, /not sorted and unique/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
