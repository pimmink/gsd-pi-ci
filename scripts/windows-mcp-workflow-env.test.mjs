import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const workflow = readFileSync(new URL("../.github/workflows/remote-pr-verification-sharded.yml", import.meta.url), "utf8");
const profile = readFileSync(new URL("./windows-mcp-profile.cjs", import.meta.url), "utf8");

function stepBody(name) {
  const start = workflow.indexOf(`      - name: ${name}`);
  assert.notEqual(start, -1, `missing workflow step: ${name}`);
  const next = workflow.indexOf("\n      - name:", start + 1);
  return workflow.slice(start, next === -1 ? workflow.length : next);
}

test("relocates temp storage only for the normal uninstrumented Windows MCP validation", () => {
  const normal = stepBody("Validate uninstrumented MCP package at unchanged process budget");
  assert.match(normal, /TEMP: \$\{\{ runner\.temp \}\}/);
  assert.match(normal, /TMP: \$\{\{ runner\.temp \}\}/);
  assert.match(normal, /TMPDIR: \$\{\{ runner\.temp \}\}/);
  assert.match(normal, /windows-mcp-profile\.cjs dist-test\/packages\/mcp-server\/src --no-profile/);

  for (const diagnostic of [
    "Diagnose MCP registry worker with detailed TAP",
    "Diagnose performance baseline without sibling test contention",
    "Profile STATE.md render causal buckets (diagnostic-only, single file)",
    "Profile native addon calls and temp-storage paths (diagnostic-only, single file)",
  ]) {
    const body = stepBody(diagnostic);
    assert.doesNotMatch(body, /runner\.temp/);
  }
});

test("preserves the paired-comparison baseline and full test budget", () => {
  assert.match(profile, /timeout: 180000/);
  assert.match(workflow, /--test-timeout=30000/);
  assert.match(workflow, /no hardcoded glob list/);
  assert.match(workflow, /const files = new Set\(\);/);
});
