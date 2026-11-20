"""Bounded safety/invariant checks; no attack interleavings, build or native acceptance."""
import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_cmux as native
import native_cmux_providers as providers

ROOT = Path(__file__).resolve().parents[2]


class DiagnosticAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="native-defensive-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.archive = self.root / "cmux-build-only.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("fixture.txt", b"ordinary fixture")
        self.original = self.archive.read_bytes()
        self.proof = {"mode": "package", "ok": True, "archive_verified": True,
                      "archive": self.archive.name, "bytes": len(self.original),
                      "sha256": hashlib.sha256(self.original).hexdigest()}
        (self.root / "package.json").write_text(json.dumps(self.proof))
        (self.root / "results.json").write_text('{"scope":"fixture diagnostics"}')

    def finalize(self, outcome):
        with patch.dict(os.environ, {"PACKAGE_OUTCOME": outcome}):
            return native.finalize_evidence(self.root)

    def test_failed_or_unknown_package_never_admits_archive_even_with_receipt(self):
        for outcome in ("failure", "cancelled", "skipped", ""):
            with self.subTest(outcome=outcome):
                self.finalize(outcome)
                self.assertEqual(self.archive.read_bytes(), self.original)
                self.assertFalse((self.root / "upload" / self.archive.name).exists())
                self.assertTrue((self.root / "upload/package.json").is_file())
                # This directory was created by this case and contains only its fixtures.
                import shutil
                shutil.rmtree(self.root / "upload")

    def test_failed_receipt_excludes_archive_and_retains_failure_diagnostics(self):
        self.proof["ok"] = False
        (self.root / "package.json").write_text(json.dumps(self.proof))
        self.finalize("success")
        self.assertFalse((self.root / "upload" / self.archive.name).exists())
        self.assertFalse(json.loads((self.root / "upload/package.json").read_text())["ok"])
        self.assertEqual(self.archive.read_bytes(), self.original)

    def test_diagnostics_never_admit_archive_even_when_package_succeeded(self):
        self.finalize("success")
        self.assertFalse((self.root / "upload" / self.archive.name).exists())
        manifest = json.loads((self.root / "upload/artifact-files.json").read_text())
        self.assertFalse(any(row["path"].endswith(".zip") for row in manifest["files"]))
        self.assertEqual(manifest["archive"], "never admitted by diagnostics")

    def test_missing_or_mismatched_proof_keeps_public_bytes_outside_upload(self):
        self.proof["sha256"] = "0" * 64
        (self.root / "package.json").write_text(json.dumps(self.proof))
        self.finalize("success")
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertFalse((self.root / "upload" / self.archive.name).exists())

    def test_unknown_files_are_not_diagnostic_upload_inputs(self):
        (self.root / "extra.json").write_text('{"not":"allowlisted"}')
        (self.root / "historical.zip").write_bytes(b"not an admitted package")
        self.finalize("failure")
        self.assertFalse((self.root / "upload/extra.json").exists())
        self.assertFalse((self.root / "upload/historical.zip").exists())
        self.assertEqual((self.root / "historical.zip").read_bytes(), b"not an admitted package")

    def test_diagnostic_budget_failure_does_not_remove_public_archive(self):
        with patch.object(native, "MAX_UPLOAD", 1), self.assertRaises(ValueError):
            self.finalize("failure")
        self.assertEqual(self.archive.read_bytes(), self.original)

    def test_cli_stages_final_bounded_failure_diagnostics(self):
        with patch.dict(os.environ, {"PACKAGE_OUTCOME": "failure"}), \
             patch.object(sys, "argv", ["native_cmux.py", "diagnostics", "--output", str(self.root)]), \
             patch.object(native, "collect_build_logs"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(native.main(), 0)
        public = json.loads((self.root / "diagnostics.json").read_text())
        self.assertTrue(public["ok"])
        self.assertEqual(public, json.loads((self.root / "upload/diagnostics.json").read_text()))
        self.assertFalse((self.root / "upload" / self.archive.name).exists())

    def test_prior_upload_directory_is_not_reused_or_removed(self):
        (self.root / "upload").mkdir()
        prior = self.root / "upload/prior.txt"
        prior.write_bytes(b"historical")
        with self.assertRaises(FileExistsError):
            self.finalize("failure")
        self.assertEqual(prior.read_bytes(), b"historical")


class PrivatePackagingTests(unittest.TestCase):
    def test_rejection_cleans_only_private_work_and_preserves_public_file(self):
        with tempfile.TemporaryDirectory(prefix="native-private-") as temp:
            root = Path(temp).resolve()
            public = root / "cmux-build-only.zip"
            public.write_bytes(b"historical public bytes")
            private = []

            def reject(app, output, report, archive):
                self.assertNotEqual(archive.parent, root)
                self.assertEqual(archive.parent.parent, root)
                private.append(archive.parent)
                archive.write_bytes(b"unaccepted private work")
                raise ValueError("fixture rejection")

            with patch.object(native, "_package_app", side_effect=reject), self.assertRaises(ValueError):
                native.package_app(root / "fixture.app", root, {})
            self.assertEqual(public.read_bytes(), b"historical public bytes")
            self.assertTrue(private)
            self.assertFalse(private[0].exists())

    def test_publication_occurs_only_after_private_producer_returns(self):
        with tempfile.TemporaryDirectory(prefix="native-private-") as temp:
            root = Path(temp).resolve()
            public = root / "cmux-build-only.zip"

            def accepted(app, output, report, archive):
                self.assertFalse(public.exists())
                archive.write_bytes(b"accepted fixture bytes")

            with patch.object(native, "_package_app", side_effect=accepted):
                native.package_app(root / "fixture.app", root, {})
            self.assertEqual(public.read_bytes(), b"accepted fixture bytes")
            self.assertEqual(public.stat().st_nlink, 1)


class ProbeFailureTests(unittest.TestCase):
    def test_only_enoent_establishes_absence(self):
        path = Mock()
        path.lstat.side_effect = FileNotFoundError(errno.ENOENT, "fixture absent")
        self.assertIsNone(providers.iroh_probe_stat(path))
        for error in (errno.EIO, errno.EACCES, errno.ENOTDIR, errno.ELOOP):
            path.lstat.side_effect = OSError(error, "fixture lookup failure")
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, "absence is unproven"):
                providers.iroh_probe_stat(path)

    def test_unproven_probe_rejects_selection_unless_force_key_is_present(self):
        text = (ROOT / "scripts/tests/fixtures/iroh-ee19-Package.swift").read_text()
        with tempfile.TemporaryDirectory(prefix="native-probe-") as temp, patch.dict(os.environ):
            os.environ.pop("IROH_FORCE_REMOTE_XCFRAMEWORK", None)
            with providers._InputBindings() as bindings, \
                 patch.object(providers, "iroh_probe_stat", side_effect=ValueError("absence is unproven")) as probe:
                with self.assertRaisesRegex(ValueError, "absence is unproven"):
                    providers.binary_targets(text, "IrohLib", checkout=Path(temp).resolve(), bindings=bindings)
                self.assertTrue(probe.called)
            os.environ["IROH_FORCE_REMOTE_XCFRAMEWORK"] = ""
            with providers._InputBindings() as bindings, \
                 patch.object(providers, "iroh_probe_stat", side_effect=AssertionError("inactive probe")) as probe:
                selected = providers.binary_targets(text, "IrohLib", checkout=Path(temp).resolve(), bindings=bindings)
                self.assertEqual(len(selected), 1)
                probe.assert_not_called()

    def test_successful_metadata_is_preserved(self):
        observed = object()
        path = Mock()
        path.lstat.return_value = observed
        self.assertIs(providers.iroh_probe_stat(path), observed)


class SealedAppAdmissionTests(unittest.TestCase):
    def setUp(self):
        fixture = DiagnosticAdmissionTests("test_failed_receipt_excludes_archive_and_retains_failure_diagnostics")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.archive, self.proof = fixture.root, fixture.archive, fixture.proof
        self.proof.update(inventory_schema=1, expected_source_sha=native.SOURCE_SHA)
        (self.root / "package.json").write_text(json.dumps(self.proof))

    def run_mode(self, mode, package="success", diagnostics="success"):
        outputs = self.root / "github-output"
        outputs.write_text("")
        with patch.dict(os.environ, {"PACKAGE_OUTCOME": package, "DIAGNOSTICS_OUTCOME": diagnostics,
                                     "GITHUB_OUTPUT": str(outputs)}), \
             patch.object(sys, "argv", ["native_cmux.py", mode, "--tag", "d014-seal-fixture", "--output", str(self.root)]), \
             contextlib.redirect_stdout(io.StringIO()):
            code = native.main()
        return code, outputs.read_text()

    def diagnostics(self):
        native.capture_build_stdout(io.BytesIO(b"bounded fixture stdout"), self.root)
        collect = native.collect_build_logs
        with patch.object(native, "collect_build_logs", side_effect=lambda output, tag:
                          collect(output, tag, temp_root=self.root / "native-log-input")):
            self.assertEqual(self.run_mode("diagnostics")[0], 0)
        self.assertFalse(any(p.suffix == ".zip" for p in (self.root / "upload").iterdir()))

    def test_provider_failure_and_bounded_zig_log_retained_without_zip(self):
        reason = "offline fixture provider collection failure"
        with patch.object(providers, "collect", side_effect=ValueError(reason)), \
             patch.object(sys, "argv", ["native_cmux_providers.py", "--source", str(self.root / "source"),
                                        "--derived-data", str(self.root / "derived"),
                                        "--output", str(self.root / "providers.json")]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(providers.main(), 1)
        self.assertFalse((self.root / "providers.json").exists())
        raw = b"discarded Zig installer fixture prefix\n" * 32 + b"Zig fixture complete\n"
        with patch.object(native, "LOG_LIMIT", 256):
            zig = native.retain_log(raw, self.root, "zig-setup.log", len(raw), "offline installer stdout fixture")
            (self.root / "tools.json").write_text(json.dumps({"ok": False, "zig_setup_stdout": zig}))
            self.diagnostics()
        stage = self.root / "upload"
        failure = json.loads((stage / "providers-collection.json").read_text())
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["error"], reason)
        tools = json.loads((stage / "tools.json").read_text())
        self.assertEqual(tools["zig_setup_stdout"]["path"], "zig-setup.log")
        self.assertTrue(zig["truncated"])
        self.assertEqual((stage / "zig-setup.log").read_bytes(), raw[-256:])
        self.assertLessEqual((stage / "zig-setup.log").stat().st_size, 256)
        manifest = json.loads((stage / "artifact-files.json").read_text())
        rows = {row["path"]: row for row in manifest["files"]}
        for name in ("providers-collection.json", "zig-setup.log"):
            self.assertEqual(rows[name]["sha256"], native.sha256(stage / name))
            self.assertEqual(rows[name]["sha256"], native.sha256(self.root / name))
            self.assertEqual(rows[name]["bytes"], (stage / name).stat().st_size)
        self.assertTrue((self.root / "cmux-build-only.zip").is_file())
        self.assertFalse(any(p.suffix == ".zip" for p in stage.iterdir()))
        self.assertNotIn("cmux-build-only.zip", rows)
        self.assertEqual((self.root / "github-output").read_text(), "")

    def test_accepted_package_exports_only_readonly_same_byte_staging(self):
        self.diagnostics()
        code, outputs = self.run_mode("seal")
        self.assertEqual(code, 0)
        proof = json.loads((self.root / "seal.json").read_text())
        retained = Path(proof["archive_path"])
        self.assertTrue(proof["verified_seal"])
        self.assertIn("verified=true\n", outputs)
        self.assertIn("archive_path=" + str(retained), outputs)
        self.assertEqual(native.sha256(retained), self.proof["sha256"])
        self.assertEqual(retained.read_bytes(), self.archive.read_bytes())
        self.assertNotEqual(retained.stat().st_ino, self.archive.stat().st_ino)
        self.assertEqual(retained.stat().st_mode & 0o222, 0)
        self.assertEqual(retained.parent.stat().st_mode & 0o222, 0)

    def test_rejected_package_step_exports_no_app(self):
        self.diagnostics()
        code, outputs = self.run_mode("seal", package="failure")
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        self.assertFalse(json.loads((self.root / "seal.json").read_text())["ok"])
        self.assertFalse(any(self.root.glob("app-seal-*")))

    def test_rejected_package_receipt_exports_no_app(self):
        self.proof["ok"] = False
        (self.root / "package.json").write_text(json.dumps(self.proof))
        self.diagnostics()
        code, outputs = self.run_mode("seal")
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        self.assertFalse(json.loads((self.root / "seal.json").read_text())["verified_seal"])

    def test_mismatched_archive_rejects_without_losing_failure_logs(self):
        self.proof["sha256"] = "0" * 64
        (self.root / "package.json").write_text(json.dumps(self.proof))
        original = self.archive.read_bytes()
        self.diagnostics()
        code, outputs = self.run_mode("seal")
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual((self.root / "upload/build-stdout.log").read_bytes(), b"bounded fixture stdout")
        self.assertFalse(any(p.suffix == ".zip" for p in (self.root / "upload").iterdir()))

    def test_validation_and_cleanup_failure_cannot_emit_admission(self):
        self.diagnostics()
        with patch.object(native, "_validate_sealed_archive", side_effect=ValueError("fixture validation rejection")), \
             patch.object(native, "_discard_private_app_stage", side_effect=OSError("fixture cleanup failure")):
            code, outputs = self.run_mode("seal")
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        receipt = json.loads((self.root / "seal.json").read_text())
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["verified_seal"])
        self.assertTrue(receipt["private_cleanup_failed"])
        self.assertNotIn("archive_path", receipt)
        self.assertTrue(any(self.root.glob("app-seal-*/cmux-build-only.zip")))
        self.assertTrue((self.root / "upload/build-stdout.log").is_file())
        self.assertFalse(any(p.suffix == ".zip" for p in (self.root / "upload").iterdir()))

    def test_seal_receipt_staging_failure_exports_no_admission(self):
        self.diagnostics()
        stage_file = native._stage_evidence_file

        def retain(source, stage, name, limit, expected=None):
            if name == "seal.json":
                raise OSError("fixture seal receipt staging failure")
            return stage_file(source, stage, name, limit, expected)

        with patch.object(native, "_stage_evidence_file", side_effect=retain):
            with self.assertRaisesRegex(OSError, "seal receipt staging failure"):
                self.run_mode("seal")
        self.assertEqual((self.root / "github-output").read_text(), "")
        self.assertTrue((self.root / "upload/build-stdout.log").is_file())
        self.assertFalse(any(p.suffix == ".zip" for p in (self.root / "upload").iterdir()))

    def test_combined_upload_budget_rejects_app_without_removing_diagnostics(self):
        self.diagnostics()
        with patch.object(native, "MAX_UPLOAD", len(self.archive.read_bytes())):
            code, outputs = self.run_mode("seal")
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        self.assertTrue((self.root / "upload/diagnostics.json").is_file())

    def test_actual_package_cli_then_diagnostics_and_seal_accepts_same_bytes(self):
        import test_native_cmux as fixtures
        fixture = fixtures.ReworkTests("test_f2_private_copy_and_archive_verified")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.package(), 0)
        self.root = fixture.out
        self.archive = self.root / "cmux-build-only.zip"
        self.diagnostics()
        code, outputs = self.run_mode("seal")
        self.assertEqual(code, 0)
        proof = json.loads((self.root / "seal.json").read_text())
        self.assertTrue(proof["verified_seal"])
        self.assertEqual(native.sha256(Path(proof["archive_path"])), native.sha256(self.archive))
        self.assertIn("verified=true\n", outputs)


class WorkflowAllowlistTests(unittest.TestCase):
    def test_upload_paths_are_exact_private_allowlist_not_recursive_public_tree(self):
        text = subprocess.check_output([
            "ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))",
            str(ROOT / ".github/workflows/native-cmux-build-only.yml")], text=True)
        workflow = json.loads(text)
        self.assertEqual(set(workflow["jobs"]), {"queue-guard", "native-build"})
        steps = workflow["jobs"]["native-build"]["steps"]
        upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
        expected = set(native.DIAGNOSTIC_FILES) | {"artifact-files.json", "diagnostics.json", "seal.json"}
        self.assertEqual(set(upload["with"]["path"].splitlines()),
                         {"evidence/upload/" + name for name in expected})
        self.assertEqual({str(Path(p).parent) for p in upload["with"]["path"].splitlines()}, {"evidence/upload"})
        self.assertNotIn(".zip", upload["with"]["path"])
        self.assertIn("steps.diagnostics.outcome == 'success'", upload["if"])
        seal = next(step for step in steps if step.get("id") == "app_seal")
        self.assertEqual(seal["env"], {"PACKAGE_OUTCOME": "${{ steps.package.outcome }}",
                                      "DIAGNOSTICS_OUTCOME": "${{ steps.diagnostics.outcome }}"})
        app = next(step for step in steps if step is not upload and "actions/upload-artifact@" in step.get("uses", ""))
        self.assertEqual(app["if"], "always() && steps.package.outcome == 'success' && "
                         "steps.app_seal.outcome == 'success' && steps.app_seal.outputs.verified == 'true'")
        self.assertEqual(app["with"]["path"], "${{ steps.app_seal.outputs.archive_path }}")
        self.assertEqual(app["with"]["if-no-files-found"], "error")


if __name__ == "__main__":
    unittest.main()
