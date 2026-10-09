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
import { execFileSync, spawnSync } from "node:child_process";

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

test("ESM import of the CJS @gsd/native package is also wrapped (the actual production import shape; Module._load receives a resolved absolute path, not the bare specifier)", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-esm-"));
  try {
    const nativeDir = join(root, "node_modules", "@gsd", "native", "dist");
    mkdirSync(nativeDir, { recursive: true });
    writeFileSync(
      join(root, "node_modules", "@gsd", "native", "package.json"),
      JSON.stringify({
        name: "@gsd/native",
        version: "0.0.0",
        type: "commonjs",
        main: "dist/index.js",
        exports: { ".": "./dist/index.js", "./directory-sync": "./dist/directory-sync.js" },
      }),
    );
    writeFileSync(join(nativeDir, "index.js"), "module.exports = { noop() { return 1; } };\n");
    writeFileSync(
      join(nativeDir, "directory-sync.js"),
      "function syncDirectoryEntry(p) { return p.length; }\nmodule.exports = { syncDirectoryEntry };\n",
    );
    writeFileSync(
      join(root, "test.mjs"),
      [
        "import { syncDirectoryEntry } from '@gsd/native/directory-sync';",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'render';",
        "syncDirectoryEntry('/tmp/abc');",
        "syncDirectoryEntry('/tmp/de');",
        "",
      ].join("\n"),
    );
    writeFileSync(join(root, "package.json"), JSON.stringify({ type: "module" }));
    const profileDir = join(root, "profiles");
    mkdirSync(profileDir, { recursive: true });

    execFileSync(process.execPath, [join(root, "test.mjs")], {
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

    assert.match(log, /nativeModulesResolved=\["@gsd\/native\/dist\/directory-sync"\]/);
    assert.match(log, /call=@gsd\/native\/dist\/directory-sync#syncDirectoryEntry count=2 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /phase=render calls=2 wallMs=[\d.]+/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("gsd-pi's own dist-test workspace-alias resolution shape (no '@gsd/native' substring in the resolved path at all) is also detected and labeled", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-workspace-"));
  try {
    const nativeDir = join(root, "dist-test", "packages", "native", "dist", "directory-sync");
    mkdirSync(nativeDir, { recursive: true });
    writeFileSync(
      join(nativeDir, "index.js"),
      "function syncDirectoryEntry(p) { return p.length; }\nmodule.exports = { syncDirectoryEntry };\n",
    );
    writeFileSync(
      join(root, "test.cjs"),
      [
        "const { syncDirectoryEntry } = require('./dist-test/packages/native/dist/directory-sync/index.js');",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'render';",
        "syncDirectoryEntry('/tmp/a');",
        "syncDirectoryEntry('/tmp/b');",
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
    assert.equal(files.length, 1);
    const log = readFileSync(join(profileDir, files[0]), "utf8");

    assert.match(log, /nativeModulesResolved=\["@gsd\/native\/directory-sync"\]/);
    assert.match(log, /call=@gsd\/native\/directory-sync#syncDirectoryEntry count=2 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /phase=render calls=2 wallMs=[\d.]+/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("node:sqlite DatabaseSync/StatementSync calls are wrapped with count/wall/max attribution, never logging SQL text or row data", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-sqlite-"));
  try {
    writeFileSync(
      join(root, "test.cjs"),
      [
        "const { createRequire } = require('node:module');",
        "const r = createRequire(__filename);",
        "const mod = r('node:sqlite');",
        "const path = require('node:path');",
        "const os = require('node:os');",
        "const dbPath = path.join(os.tmpdir(), 'regression-profile-' + Date.now() + '.sqlite3');",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'fixture';",
        "const db = new mod.DatabaseSync(dbPath);",
        "db.exec('CREATE TABLE t (id INTEGER PRIMARY KEY, secretlike TEXT)');",
        "const insertStmt = db.prepare('INSERT INTO t (secretlike) VALUES (?)');",
        "insertStmt.run('do-not-log-me-sentinel');",
        "insertStmt.run('do-not-log-me-sentinel-2');",
        "globalThis.__NATIVE_PROFILE_PHASE__ = 'render';",
        "const selStmt = db.prepare('SELECT * FROM t');",
        "selStmt.all();",
        "const getStmt = db.prepare('SELECT * FROM t WHERE id = ?');",
        "getStmt.get(1);",
        "db.close();",
        "require('node:fs').unlinkSync(dbPath);",
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
    assert.equal(files.length, 1);
    const log = readFileSync(join(profileDir, files[0]), "utf8");

    assert.match(log, /sqliteWrapped=true/);
    assert.match(log, /call=DatabaseSync#constructor count=1 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=DatabaseSync#exec count=1 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=DatabaseSync#prepare count=3 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=DatabaseSync#close count=1 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=StatementSync#run count=2 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=StatementSync#get count=1 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /call=StatementSync#all count=1 wallMs=[\d.]+ maxMs=[\d.]+/);
    assert.match(log, /phase=fixture calls=\d+ wallMs=[\d.]+/);
    assert.match(log, /phase=render calls=\d+ wallMs=[\d.]+/);
    assert.doesNotMatch(log, /do-not-log-me-sentinel/, "SQL params/row data must never be logged");
    assert.doesNotMatch(log, /CREATE TABLE|INSERT INTO|SELECT \*/, "SQL text must never be logged");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("--relocate-temp overrides child TEMP/TMP/TMPDIR; absent flag leaves them inherited unchanged", () => {
  const root = mkdtempSync(join(tmpdir(), "native-profile-regression-relocate-"));
  try {
    const relocated = join(root, "relocated-temp");
    mkdirSync(relocated, { recursive: true });
    writeFileSync(
      join(root, "test.cjs"),
      [
        "const { test } = require('node:test');",
        "const assert = require('node:assert');",
        "test('reports temp env', () => {",
        "  console.error('[RELOCATE-CHECK] TEMP=' + process.env.TEMP + ' TMP=' + process.env.TMP);",
        "});",
        "",
      ].join("\n"),
    );
    // Rename to match the harness's required file-name filter.
    const testFile = join(root, "state-md-render.test.js");
    execFileSync("mv", [join(root, "test.cjs"), testFile]);

    const parentEnv = { ...process.env };
    delete parentEnv.NODE_TEST_CONTEXT;
    delete parentEnv.NODE_TEST_WORKER_ID;

    const resWith = spawnSync(process.execPath, [HARNESS, testFile, `--relocate-temp=${relocated}`], {
      cwd: root,
      env: { ...parentEnv, TEMP: join(root, "baseline-temp") },
      encoding: "utf8",
    });
    const combinedWith = `${resWith.stdout}\n${resWith.stderr}`;
    assert.match(combinedWith, new RegExp(`\\[RELOCATE-CHECK\\] TEMP=${relocated.replace(/[\\]/g, "\\\\")}`));
    assert.match(combinedWith, /relocate-temp active/);

    const resWithout = spawnSync(process.execPath, [HARNESS, testFile], {
      cwd: root,
      env: { ...parentEnv, TEMP: join(root, "baseline-temp") },
      encoding: "utf8",
    });
    const combinedWithout = `${resWithout.stdout}\n${resWithout.stderr}`;
    assert.match(combinedWithout, /relocate-temp not set; child inherits parent TEMP\/TMP unchanged/);
    assert.match(combinedWithout, new RegExp(`\\[RELOCATE-CHECK\\] TEMP=${join(root, "baseline-temp").replace(/[\\]/g, "\\\\")}`));
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
