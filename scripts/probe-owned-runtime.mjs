// Executed only by an explicit --probe-node. No CLI host, session, network or installation.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { builtinModules, createRequire, registerHooks } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = realpathSync(process.argv[2]);
const provenance = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const fail = (message) => { throw new Error(message); };
const hash = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');
if (process.platform !== 'darwin' || process.arch !== 'x64') fail('requires darwin/x64');
const [major, minor] = process.versions.node.split('.').map(Number);
if (major !== 24 || minor < 15) fail('requires Node 24.15+');
if (process.env.NODE_PATH || process.env.NODE_OPTIONS) fail('external module configuration');
const inside = (file) => {
  const actual = realpathSync(file);
  if (!actual.startsWith(root + path.sep)) fail('external module/native path refused');
  return actual;
};
const relative = (file) => path.relative(root, file).split(path.sep).join('/');
const expectedAddon = inside(path.join(root, 'native/addon/gsd_engine.darwin-x64.node'));
if (hash(expectedAddon) !== provenance.closure.native.sha256) fail('native hash mismatch');
const resolved = new Set();
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    const result = nextResolve(specifier, context);
    if (!result.url.startsWith('node:')) {
      if (!result.url.startsWith('file:')) fail('non-file module refused');
      resolved.add(relative(inside(fileURLToPath(result.url))));
    }
    return result;
  },
});
const loadedAddons = [];
const originalDlopen = process.dlopen;
process.dlopen = function(module, filename, flags) {
  const actual = inside(filename);
  const result = originalDlopen.call(this, module, actual, flags);
  loadedAddons.push({ path: relative(actual), sha256: hash(actual) });
  return result;
};
const imports = [];
const request = createRequire(path.join(root, 'package.json'));
// The verifier passes --experimental-import-meta-resolve so the parent URL is honored.
const esmResolve = (name, manifest = 'package.json') => inside(fileURLToPath(
  import.meta.resolve(name, pathToFileURL(path.join(root, manifest)).href),
));
// Resolve ESM conditions from each shipped workspace and the CLI; CJS native loads below.
// This does not claim that every optional runtime code path has executed.
for (const [name, directory] of Object.entries(provenance.internal_packages)) {
  const expected = inside(path.join(root, directory));
  const actual = esmResolve(name);
  if (!actual.startsWith(expected + path.sep)) fail('internal package resolved to registry/baseline copy');
  imports.push({ name, path: relative(actual), sha256: hash(actual) });
}
const importers = ['package.json', ...new Set(Object.values(provenance.internal_packages).map((dir) => dir + '/package.json'))];
let dependencyResolutions = 0;
for (const manifest of importers) {
  const pkg = JSON.parse(readFileSync(path.join(root, manifest), 'utf8'));
  for (const name of Object.keys(pkg.dependencies || {})) {
    if (builtinModules.includes(name)) continue;
    // The MCP SDK is consumed only through its /client and /server subpaths (see
    // mcp-client and mcp-server sources). Its bare root export is not a runtime import.
    if (name === '@modelcontextprotocol/sdk') continue;
    // Binary-only daemons (no main/exports, e.g. @opengsd/gsd-browser) are launched as
    // executables by the runtime, never imported as ESM libraries; probe them as such.
    const depDir = path.join(root, 'node_modules', name);
    if (existsSync(depDir) && statSync(depDir).isDirectory()) {
      const depPkg = JSON.parse(readFileSync(path.join(depDir, 'package.json'), 'utf8'));
      if (!depPkg.main && !depPkg.exports && depPkg.bin) continue;
    }
    esmResolve(name, manifest);
    dependencyResolutions++;
  }
}
const wrapper = request(path.join(root, 'packages/native/dist/native.js'));
if (!wrapper.isNativeAddonLoaded()) fail('native wrapper fell back');
const actualAddon = request(expectedAddon);
for (const name of ['SqliteFileIdentityLock', 'ProjectionRootIdentityLock']) {
  if (typeof wrapper.native[name] !== 'function' || wrapper.native[name] !== actualAddon[name]) {
    fail('required source-native export missing/mismatched: ' + name);
  }
}
if (!loadedAddons.some((entry) => entry.path === relative(expectedAddon) && entry.sha256 === provenance.closure.native.sha256)) {
  fail('source-built native addon was not actually loaded');
}
if (loadedAddons.some((entry) => entry.path.includes('@opengsd/engine-'))) fail('published native fallback refused');
// Import real public SDK entries without invoking CLI/MCP hosts. Guard every transitive resolution.
const importedSdkEntries = [];
// Probe the public subpaths the runtime actually imports. The MCP SDK root export
// '.' points at dist/esm/index.js, which SDK 1.32.0 does not ship; the runtime never
// imports that root, so it is not a runtime requirement.
for (const name of ['@gsd/pi-agent-core', '@opengsd/contracts', '@opengsd/rpc-client']) {
  const entry = esmResolve(name);
  await import(pathToFileURL(entry).href);
  importedSdkEntries.push({ name, path: relative(entry), sha256: hash(entry) });
}
process.dlopen = originalDlopen;
hooks.deregister();
console.log(JSON.stringify({ platform: process.platform, arch: process.arch, node: process.version, executable: process.execPath,
  native: loadedAddons, internal_imports: imports, dependency_resolutions: dependencyResolutions,
  imported_sdk_entries: importedSdkEntries, resolved_module_paths: [...resolved].sort(),
  limits: 'Import/native probe only, not host/presentation or full optional-feature acceptance.' }));
