// Regression test for scripts/windows-native-call-profile.cjs's Module._load
// proxy wrapper: proves native addon exports get count/wall/max attribution
// and phase-label bucketing without altering call behavior or return values.
// Exercises the exact mechanism (require-time wrap via Module._load, plain
// function-value descriptors only, classes/constructors left untouched) used
// against the real @gsd/native/directory-sync export, using a throwaway fake
// module under a temp node_modules tree so this test never depends on the
// real native addon being built.
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const HARNESS = fileURLToPath(new URL("./windows-native-call-profile.cjs", import.meta.url));

test("Module._load proxy wraps @gsd/native function exports with count/wall/max attribution, preserves class exports untouched", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-"));
  try {
    const nativeDir = join(root, "node_modules", "@gsd", "native", "dist");
    mkdirSync(nativeDir, { recursive: true });
    writeFileSync(
      join(root, "node_modules", "@gsd", "native", "package.json"),
      JSON.stringify({
        name: "@gsd/native",
        version: "0.0.0",
        main: "dist/index.js",
        exports: { ".": "./dist/index.js", "./directory-sync": "./dist/directory-sync.js" },
      }),
    );
    writeFileSync(join(nativeDir, "index.js"), "module.exports = { noop() { return 1; } };\n");
    writeFileSync(
      join(nativeDir, "directory-sync.js"),
      [
        "class NotAFunctionExport { constructor() { this.tag = 'lock'; } method() { return 'ok'; } }",
        "function syncDirectoryEntry(p) { return p.length; }",
        "module.exports = { syncDirectoryEntry, NotAFunctionExport };",
        "",
      ].join("\n"),
    );
    writeFileSync(
      join(root, "test.cjs"),
      [
        "const { syncDirectoryEntry, NotAFunctionExport } = require('@gsd/native/directory-sync');",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'render';",
        "for (let i = 0; i < 3; i++) syncDirectoryEntry('/tmp/abc');",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'fixture';",
        "syncDirectoryEntry('/tmp/de');",
        "const lock = new NotAFunctionExport();",
        "if (lock.method() !== 'ok') throw new Error('class export was broken by wrapping');",
        "",
      ].join("\n"),
    );
    const profileDir = join(root, "profiles");
    mkdirSync(profileDir, { recursive: true });

    execFileSync(process.execPath, [join(root, "test.cjs")], {
      cwd: root,
      env: {
        ...process.env,
        NATIVE_PROFILE_DIR: profileDir,
        NODE_OPTIONS: `--require=${JSON.stringify(HARNESS.replaceAll("\\", "/"))}`,
      },
      encoding: "utf8",
    });

    const files = readdirSync(profileDir);
    assert.equal(files.length, 1, "exactly one profile log written for the single child process");
    const log = readFileSync(join(profileDir, files[0]), "utf8");

    assert.match(log, /nativeModulesResolved=\["@gsd\/native\/directory-sync"\]/);
    assert.match(log, /call=@gsd\/native\/directory-sync#syncDirectoryEntry count=4 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /phase=render calls=3 wallMs=[\d.]+/);
    assert.match(log, /phase=fixture calls=1 wallMs=[\d.]+/);
    assert.doesNotMatch(log, /NotAFunctionExport/, "class/constructor export must never be wrapped or logged as a call");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("harness reports nativeCalls=NONE when no @gsd/native specifier is ever required", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-none-"));
  try {
    writeFileSync(join(root, "test.cjs"), "1 + 1;\n");
    const profileDir = join(root, "profiles");
    mkdirSync(profileDir, { recursive: true });

    execFileSync(process.execPath, [join(root, "test.cjs")], {
      cwd: root,
      env: {
        ...process.env,
        NATIVE_PROFILE_DIR: profileDir,
        NODE_OPTIONS: `--require=${JSON.stringify(HARNESS.replaceAll("\\", "/"))}`,
      },
      encoding: "utf8",
    });

    const files = readdirSync(profileDir);
    const log = readFileSync(join(profileDir, files[0]), "utf8");
    assert.match(log, /nativeCalls=NONE/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
