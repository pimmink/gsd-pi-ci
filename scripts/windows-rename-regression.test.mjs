// Targeted test-first contract. Run from a disposable cwd containing
// pid-registry.mjs stripped from the exact source; no application/native build.
import assert from "node:assert/strict";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
const { registerMcpInstance } = await import(pathToFileURL(resolve("pid-registry.mjs")).href);

function fixture(platform, injected, check) {
  const root = fs.mkdtempSync(join(tmpdir(), "mcp-rename-regression-"));
  const project = join(root, "project");
  fs.mkdirSync(project);
  const registry = join(root, "mcp-instances.json");
  const snapshot = JSON.stringify({
    retained: { pid: 123, projectDir: "retained", startedAt: "2020-01-01T00:00:00Z" },
  });
  fs.writeFileSync(registry, snapshot);
  const originalRename = fs.renameSync;
  const originalPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  let calls = 0;
  try {
    Object.defineProperty(process, "platform", { ...originalPlatform, value: platform });
    fs.renameSync = (oldPath, newPath) => {
      calls++;
      const error = injected(calls);
      if (error) throw error;
      return originalRename(oldPath, newPath);
    };
    syncBuiltinESMExports();
    check({
      run: () => registerMcpInstance(project, registry),
      registry,
      snapshot,
      calls: () => calls,
      root,
    });
  } finally {
    fs.renameSync = originalRename;
    Object.defineProperty(process, "platform", originalPlatform);
    syncBuiltinESMExports();
    fs.rmSync(root, { recursive: true, force: true });
  }
}
function sharing(code) {
  return Object.assign(new Error(`simulated ${code} rename contention`), { code });
}

for (const code of ["EPERM", "EACCES", "EBUSY"]) {
  test(`Windows ${code}: two transient failures still commit one complete snapshot`, () => {
    fixture(
      "win32",
      (count) => (count <= 2 ? sharing(code) : null),
      ({ run, registry, calls }) => {
        assert.doesNotThrow(run);
        assert.equal(calls(), 3);
        const contents = JSON.parse(fs.readFileSync(registry, "utf8"));
        assert.equal(Object.keys(contents).length, 2);
        assert.equal(contents.retained.projectDir, "retained");
      },
    );
  });
}

test("Persistent Windows EPERM fails bounded, preserves exact old bytes and cleans its own temp", () => {
  const error = sharing("EPERM");
  fixture(
    "win32",
    () => error,
    ({ run, registry, snapshot, calls, root }) => {
      assert.throws(run, (value) => value === error);
      assert.ok(calls() >= 1 && calls() <= 12, `unbounded attempts: ${calls()}`);
      assert.equal(fs.readFileSync(registry, "utf8"), snapshot);
      assert.ok(!fs.readdirSync(root).some((name) => name.endsWith(".tmp")));
    },
  );
});

test("Windows permanent EIO is not retried or suppressed", () => {
  const error = sharing("EIO");
  fixture(
    "win32",
    () => error,
    ({ run, registry, snapshot, calls }) => {
      assert.throws(run, (value) => value === error);
      assert.equal(calls(), 1);
      assert.equal(fs.readFileSync(registry, "utf8"), snapshot);
    },
  );
});

test("POSIX EPERM remains a real permission failure without retries", () => {
  const error = sharing("EPERM");
  fixture(
    "darwin",
    () => error,
    ({ run, registry, snapshot, calls }) => {
      assert.throws(run, (value) => value === error);
      assert.equal(calls(), 1);
      assert.equal(fs.readFileSync(registry, "utf8"), snapshot);
    },
  );
});
