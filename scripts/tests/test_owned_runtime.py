"""Offline synthetic tests only: no builds, installs, downloads, native host or GitHub."""
import gzip
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import owned_runtime as rt

SOURCE_SHA = "a" * 40
WORKFLOW_SHA = "b" * 40


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # Even scratch data is owned by this worktree, never the sourcefix checkout.
        self.temp = tempfile.TemporaryDirectory(dir=SCRIPTS / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.bundle = self.root / "bundle"

    def put(self, rel, content="fixture", symlink=None):
        dest = self.source / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if symlink:
            dest.symlink_to(symlink)
        else:
            dest.write_text(content)
        return dest

    def fixture(self):
        self.put("package.json", json.dumps({"name": "@opengsd/gsd-pi", "files": ["dist", "packages/*/dist", "packages/*/bin", "packages/*/package.json"]}))
        self.put("pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self.put("pnpm-workspace.yaml", "packages: ['packages/*']\n")
        self.put("dist/bootstrap.js", "export {};\n")
        self.put("dist/loader.js", "export {};\n")
        for name in ("native", "mcp-server"):
            pkg = {"name": "@gsd/native" if name == "native" else "@opengsd/mcp-server", "main": "dist/index.js", "gsd": {"linkable": True}}
            self.put(f"packages/{name}/package.json", json.dumps(pkg))
            self.put(f"packages/{name}/dist/index.js", "export {};\n")
        self.put("packages/native/dist/native.js", "exports.isNativeAddonLoaded = () => false;\n")
        self.put("packages/mcp-server/bin/gsd-mcp-server.js", "fixture only\n")
        self.put("native/addon/gsd_engine.darwin-x64.node", "not an actual native binary\n")
        self.put("node_modules/.pnpm/example@1/node_modules/example/index.js", "exports.example = 1;\n")
        self.put("node_modules/example", symlink=".pnpm/example@1/node_modules/example")
        self.put("packages/native/node_modules/example", symlink="../../../node_modules/.pnpm/example@1/node_modules/example")
        self.put("node_modules/@gsd/native", symlink=str(self.source / "packages/native"))
        self.put("node_modules/.cache/secret", "never copy")
        self.put("node_modules/.env", "never copy")
        self.put("node_modules/.npmrc", "never copy")
        self.put("node_modules/.bin/fixture", "absolute runner paths excluded")
        return self

    def context(self):
        return {"expected_sha": SOURCE_SHA, "source_ref": "fix/owned", "observed_branch_head": "c" * 40,
                "observed_at": "2026-11-11T00:00:00Z", "workflow_sha": WORKFLOW_SHA,
                "workflow_ref": "pimmink/gsd-pi-ci/.github/workflows/remote-pr-verification.yml@refs/heads/ci/candidate",
                "run_id": "123", "attempt": "2", "build": {"fixture": True,
                    "native_libraries": "addon:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n",
                    "cargo_encoded_rustflags": "", "rustflags": ""}}

    def package(self, context=None):
        def git(args, cwd):
            if args[1:] == ["rev-parse", "HEAD"]:
                return SOURCE_SHA
            if args[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "HEAD"
            if args[1:] == ["rev-parse", "HEAD^{tree}"]:
                return "d" * 40
            if args[1] == "ls-files":
                return "package.json\0pnpm-lock.yaml\0pnpm-workspace.yaml\0"
            if args[1] == "diff":
                return ""
            raise AssertionError(args)
        with patch.object(rt, "command", side_effect=git):
            return rt.package_runtime(self.source, self.bundle, context or self.context())

    def verify(self):
        return rt.verify_bundle(self.bundle, SOURCE_SHA, WORKFLOW_SHA, "123", "2")

    def sums(self):
        (self.bundle / "SHA256SUMS").write_text("".join(f"{rt.digest(self.bundle / n)}  {n}\n" for n in rt.PAYLOADS))

    def tar(self, members, fmt=tarfile.GNU_FORMAT):
        archive = self.root / "malicious.tar.gz"
        with tarfile.open(archive, "w:gz", format=fmt) as f:
            for name, kind, value in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = value
                elif kind == tarfile.REGTYPE:
                    info.size = len(value)
                f.addfile(info, io.BytesIO(value) if kind == tarfile.REGTYPE else None)
        return archive

    def test_roundtrip_full_closure_and_independent_pins(self):
        self.fixture()
        p = self.package()
        self.assertNotEqual(p["source"]["sha"], p["source"]["observed_branch_head"])
        self.assertNotEqual(p["source"]["sha"], p["workflow"]["sha"])
        self.verify()
        dest = self.root / "unpacked"
        rt.extract_bundle(self.bundle, dest, SOURCE_SHA, WORKFLOW_SHA, "123", "2")
        runtime = dest / "runtime"
        self.assertEqual((runtime / "node_modules/@gsd/native").resolve(), runtime / "packages/native")
        self.assertTrue((runtime / "packages/native/node_modules/example/index.js").is_file())
        for name in (".cache", ".env", ".npmrc", ".bin"):
            self.assertFalse((runtime / "node_modules" / name).exists())
        self.assertEqual(rt.inventory(runtime), rt.inventory(self.bundle / "runtime"))
        with self.assertRaisesRegex(ValueError, "destination must be new"):
            rt.extract_bundle(self.bundle, dest, SOURCE_SHA, WORKFLOW_SHA, "123", "2")

    def test_long_gnu_path_and_link_roundtrip(self):
        self.fixture()
        folder = "node_modules/" + "x" * 150
        self.put(folder + "/payload", "long path")
        self.put("node_modules/long", symlink="x" * 150)
        self.package()
        self.verify()

    def test_wrong_pins_fail_closed(self):
        self.fixture().package()
        for pins in [("f" * 40, WORKFLOW_SHA, "123", "2"), (SOURCE_SHA, "f" * 40, "123", "2"),
                     (SOURCE_SHA, WORKFLOW_SHA, "124", "2"), (SOURCE_SHA, WORKFLOW_SHA, "123", "3")]:
            with self.subTest(pins=pins), self.assertRaises(ValueError):
                rt.verify_bundle(self.bundle, *pins)
        for value in ("a" * 39, "A" * 40, "a" * 41, "a" * 39 + "\n", "--help"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rt.sha(value)

    def test_wrong_source_head_rejected_before_output(self):
        self.fixture()
        ctx = self.context()
        ctx["expected_sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "source HEAD mismatch"):
            self.package(ctx)
        self.assertFalse(self.bundle.exists())

    def test_corrupted_archive_or_metadata(self):
        self.fixture().package()
        for name in rt.PAYLOADS:
            path = self.bundle / name
            original = path.read_bytes()
            path.write_bytes(original + b"tampering")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.verify()
            path.write_bytes(original)

    def test_self_consistent_manifest_tamper_still_fails_content(self):
        self.fixture().package()
        path = self.bundle / "file-manifest.json"
        manifest = json.loads(path.read_bytes())
        next(e for e in manifest["entries"] if e["type"] == "file")["sha256"] = "0" * 64
        path.write_bytes(rt.json_bytes(manifest))
        p = json.loads((self.bundle / "provenance.json").read_bytes())
        p["manifest_sha256"] = rt.digest(path)
        (self.bundle / "provenance.json").write_bytes(rt.json_bytes(p))
        self.sums()
        with self.assertRaisesRegex(ValueError, "archive manifest mismatch"):
            self.verify()

    def test_archive_path_type_and_link_security(self):
        cases = [("/tmp/owned-escape", tarfile.REGTYPE, b"bad"),
                 ("runtime/../escape", tarfile.REGTYPE, b"bad"),
                 ("runtime//escape", tarfile.REGTYPE, b"bad"),
                 ("runtime/C:\\escape", tarfile.REGTYPE, b"bad"),
                 ("runtime/line\nbreak", tarfile.REGTYPE, b"bad"),
                 ("runtime/link", tarfile.SYMTYPE, "/tmp"),
                 ("runtime/link", tarfile.SYMTYPE, "../../outside"),
                 ("runtime/hardlink", tarfile.LNKTYPE, "runtime/file"),
                 ("runtime/device", tarfile.CHRTYPE, ""),
                 ("runtime/block", tarfile.BLKTYPE, ""),
                 ("runtime/fifo", tarfile.FIFOTYPE, ""),
                 ("runtime/sparse", tarfile.GNUTYPE_SPARSE, "")]
        for item in cases:
            with self.subTest(item=item), self.assertRaises((ValueError, tarfile.TarError)):
                rt.archive_entries(self.tar([("runtime", tarfile.DIRTYPE, ""), item]))

    def test_duplicates_case_collision_parent_links_cycles_and_dangling(self):
        cases = [
            [("runtime/a", tarfile.REGTYPE, b"a"), ("runtime/a", tarfile.REGTYPE, b"a")],
            [("runtime/A", tarfile.REGTYPE, b"a"), ("runtime/a", tarfile.REGTYPE, b"a")],
            [("runtime/café", tarfile.REGTYPE, b"a"), ("runtime/cafe\u0301", tarfile.REGTYPE, b"a")],
            [("runtime/link", tarfile.SYMTYPE, "."), ("runtime/link/child", tarfile.REGTYPE, b"a")],
            [("runtime/a", tarfile.SYMTYPE, "b"), ("runtime/b", tarfile.SYMTYPE, "a")],
            [("runtime/a", tarfile.SYMTYPE, "missing")],
            [("runtime/deep", tarfile.DIRTYPE, ""), ("runtime/deep/link", tarfile.SYMTYPE, ".."),
             ("runtime/escape", tarfile.REGTYPE, b"a"), ("runtime/a", tarfile.SYMTYPE, "deep/link/../../escape")],
        ]
        for members in cases:
            with self.subTest(members=members), self.assertRaises(ValueError):
                rt.archive_entries(self.tar([("runtime", tarfile.DIRTYPE, "")] + members))

    def test_pax_sparse_special_modes_and_limits(self):
        archive = self.tar([("runtime", tarfile.DIRTYPE, ""), ("runtime/" + "x" * 150, tarfile.REGTYPE, b"a")], tarfile.PAX_FORMAT)
        with self.assertRaisesRegex(ValueError, "PAX"):
            rt.archive_entries(archive)
        with gzip.open(archive, "wb") as f:
            info = tarfile.TarInfo("runtime/long")
            info.type = tarfile.GNUTYPE_LONGNAME
            info.size = 99999999
            f.write(info.tobuf(format=tarfile.GNU_FORMAT))
        with self.assertRaisesRegex(ValueError, "long header"):
            rt.archive_entries(archive)
        archive = self.tar([("runtime", tarfile.DIRTYPE, ""), ("runtime/file", tarfile.REGTYPE, b"12345")])
        with patch.object(rt, "MAX_BYTES", 4), self.assertRaisesRegex(ValueError, "size limit"):
            rt.archive_entries(archive)
        with patch.object(rt, "MAX_MEMBERS", 1), self.assertRaisesRegex(ValueError, "expansion limit"):
            rt.archive_entries(archive)
        with patch.object(rt, "MAX_ARCHIVE", 1), self.assertRaisesRegex(ValueError, "archive size"):
            rt.archive_entries(archive)
        with tarfile.open(archive, "w:gz") as f:
            info = tarfile.TarInfo("runtime")
            info.type = tarfile.DIRTYPE
            info.mode = 0o4755
            f.addfile(info)
        with self.assertRaisesRegex(ValueError, "special mode"):
            rt.archive_entries(archive)

    def test_invalid_archive_never_creates_destination(self):
        self.fixture().package()
        archive = self.bundle / "runtime.tar.gz"
        archive.write_bytes(self.tar([("../escaped", tarfile.REGTYPE, b"bad")]).read_bytes())
        self.sums()
        dest = self.root / "unpacked"
        with self.assertRaises(ValueError):
            rt.extract_bundle(self.bundle, dest, SOURCE_SHA, WORKFLOW_SHA, "123", "2")
        self.assertFalse(dest.exists())
        self.assertFalse((self.root / "escaped").exists())

    def test_external_source_links_and_missing_build_fail(self):
        self.fixture()
        self.put("node_modules/external", symlink=str(self.root))
        with self.assertRaisesRegex(ValueError, "source link escapes"):
            self.package()

    def test_native_external_dylib_and_nonportable_flags_rejected(self):
        self.fixture()
        for change in ({"native_libraries": "addon:\n\t/usr/local/opt/runner/libbad.dylib\n"},
                       {"rustflags": "-C target-cpu=native"}, {"cargo_encoded_rustflags": "-Ctarget-cpu=native"}):
            ctx = self.context()
            ctx["build"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.package(ctx)
            self.assertFalse(self.bundle.exists())

    def test_missing_addon_not_treated_as_npm_closure(self):
        self.fixture()
        (self.source / "native/addon/gsd_engine.darwin-x64.node").unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.package()

    def test_probe_cannot_pass_synthetic_non_native_closure(self):
        self.fixture().package()
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for helper tests, not a production host")
        result = subprocess.run([sys.executable, str(SCRIPTS / "verify-owned-runtime.py"),
            "--bundle", str(self.bundle), "--expected-sha", SOURCE_SHA, "--workflow-sha", WORKFLOW_SHA,
            "--run-id", "123", "--attempt", "2", "--destination", str(self.root / "probe-unpacked"),
            "--probe-node", node], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_verify_only_cli_executes_no_runtime(self):
        self.fixture().package()
        out = subprocess.check_output([sys.executable, str(SCRIPTS / "verify-owned-runtime.py"),
            "--bundle", str(self.bundle), "--expected-sha", SOURCE_SHA, "--workflow-sha", WORKFLOW_SHA,
            "--run-id", "123", "--attempt", "2"], text=True, timeout=10)
        receipt = json.loads(out)
        self.assertTrue(receipt["all_bytes_verified"])
        self.assertFalse(receipt["extracted"])
        self.assertIsNone(receipt["runtime_probe"])


class WorkflowContractTests(unittest.TestCase):
    def test_opt_in_pins_permissions_retention_and_no_cancellation(self):
        workflow = (SCRIPTS.parent / ".github/workflows/remote-pr-verification.yml").read_text()
        self.assertIn("default: false", workflow)
        self.assertIn("cancel-in-progress: ${{ !inputs.owned_runtime_only }}", workflow)
        self.assertIn("github.run_id, github.run_attempt", workflow)
        ordinary, owned = workflow.split("\n  owned-runtime:\n", 1)
        self.assertIn('if [[ "$remote_sha" != "$EXPECTED_SHA" ]]', ordinary)
        self.assertNotIn('"$observed_sha" == "$EXPECTED_SHA"', owned)
        self.assertIn('fetch --no-tags --depth=1 origin "$EXPECTED_SHA"', owned)
        self.assertIn('fetch --no-tags --depth=1 origin "$WORKFLOW_SHA"', owned)
        self.assertIn("runs-on: macos-15-intel", owned)
        self.assertIn("retention-days: 14", owned)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("secrets.", owned)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("CARGO_ENCODED_RUSTFLAGS: ''", owned)
        self.assertIn("cargo build --release --locked --target x86_64-apple-darwin", owned)
        self.assertNotIn("pnpm pack", owned)


if __name__ == "__main__":
    unittest.main()
