"""PR2 A/B regressions; real package/fence routes, mocked Apple tools only.

The Iroh manifest fixture is verbatim manaflow-ai/iroh-ffi Package.swift at
revision ee19f156667ca640b912108a45f8b5bb8d156fec. Its commit/tree/blob chain
was verified with production git_blob; distribution fixtures below remain
synthetic and are never represented as actual release/artifact evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_cmux as native
import native_cmux_providers as providers
import test_native_cmux as fixtures


MANIFEST = Path(__file__).with_name("fixtures") / "iroh-ee19-Package.swift"
MANIFEST_SHA256 = "176b8a1e0b51eb76392055f94c3c7dc77fc1805d9e8be689299e8801810409a9"
REMOTE = {
    "name": "Iroh",
    "url": "https://github.com/manaflow-ai/iroh-ffi/releases/download/v1.2.0-cmux.1.ios17/IrohLib.xcframework.zip",
    "checksum": "9a30e7b639097631779670bd01d52acbd5d57bf3a35f8b81aa9b5ecf9c496dfa",
}


def manifest():
    data = MANIFEST.read_bytes()
    assert hashlib.sha256(data).hexdigest() == MANIFEST_SHA256
    return data.decode()


def remote_only_manifest():
    """Isolate the exact real byName wrapper from the independently tested condition."""
    text = manifest()
    start = text.index("let irohBinary: Target = useLocalXcframework")
    remote = text.index("    : .binaryTarget(", start)
    end = text.index("\n\nlet package =", remote)
    return text[:start] + "let irohBinary: Target = " + text[remote + 6:end] + text[end:]


class ProductionPackagingCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReworkTests("test_f2_private_copy_and_archive_verified")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.archive = self.fixture.out / "cmux-build-only.zip"

    def test_outer_real_directory_fence_rejection_removes_only_attempt_zip(self):
        historical = self.fixture.out / "historical.zip"
        historical.write_bytes(b"not this attempt")

        def ancestor_aba(*args):
            self.fixture.fake_ditto(*args)
            ancestor = self.fixture.app.parent
            moved = ancestor.with_name(ancestor.name + "-abc-held")
            self.assertFalse(moved.exists())
            ancestor.rename(moved)
            moved.rename(ancestor)
            return ""

        self.assertEqual(self.fixture.package(ancestor_aba), 1)
        report = json.loads((self.fixture.out / "package.json").read_text())
        self.assertFalse(report["ok"])
        self.assertIn("Directory/ancestor", report["error"])
        self.assertFalse(os.path.lexists(self.archive), "rejected attempt ZIP must not remain uploadable")
        self.assertEqual(historical.read_bytes(), b"not this attempt")

    def test_valid_production_packaging_still_publishes_verified_zip(self):
        self.assertEqual(self.fixture.package(), 0)
        report = json.loads((self.fixture.out / "package.json").read_text())
        self.assertTrue(report["ok"])
        self.assertTrue(report["archive_verified"])
        self.assertEqual(native.sha256(self.archive), report["sha256"])

    def test_existing_historical_archive_is_not_deleted(self):
        self.archive.write_bytes(b"historical bytes")
        self.assertEqual(self.fixture.package(), 1)
        self.assertEqual(self.archive.read_bytes(), b"historical bytes")

    def test_foreign_archive_arriving_during_production_is_not_overwritten_or_deleted(self):
        def foreign_arrival(*args):
            self.fixture.fake_ditto(*args)
            self.archive.write_bytes(b"foreign archive, not this invocation")
            return ""
        self.assertEqual(self.fixture.package(foreign_arrival), 1)
        self.assertTrue(self.archive.exists())
        self.assertEqual(self.archive.read_bytes(), b"foreign archive, not this invocation")

    def test_archive_is_private_until_verification_finishes(self):
        verify = native.verify_archive

        def assert_private(*args):
            self.assertFalse(self.archive.exists())
            self.assertNotEqual(Path(args[0]).parent, self.fixture.out)
            verify(*args)

        with patch.object(native, "verify_archive", side_effect=assert_private):
            self.assertEqual(self.fixture.package(), 0)
        self.assertTrue(self.archive.is_file())

    def test_existing_dangling_archive_link_is_not_followed_or_deleted(self):
        target = self.fixture.out / "absent-historical.zip"
        self.archive.symlink_to(target.name)
        self.assertEqual(self.fixture.package(), 1)
        self.assertTrue(self.archive.is_symlink())
        self.assertFalse(target.exists())


class PinnedIrohManifestTests(unittest.TestCase):
    def test_actual_remote_variant_reaches_distribution_checksum_validation(self):
        fixture = fixtures.ReworkTests("test_f2_private_copy_and_archive_verified")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        case = fixture.fixture
        checkout = case.provider_packages / "checkouts/iroh-ffi"
        text = manifest().encode()
        case.provider_manifests["iroh-ffi"] = text
        (checkout / "Package.swift").write_bytes(text)
        # Actual locked manifest/checksum, synthetic cached archives: reaching this
        # error proves the selected remote branch and byName wrapper were parsed,
        # not that a real Iroh distribution/build was accepted.
        with patch.dict(os.environ, {}, clear=False):
            with self.assertRaisesRegex(ValueError, "missing/ambiguous checksum-verified"):
                providers.derive(case.provider_source, case.provider_packages, case.provider_archives)

    def test_exact_real_byname_wrapper_resolves_remote_binary(self):
        try:
            targets = providers.binary_targets(remote_only_manifest(), "IrohLib")
        except ValueError as exc:
            self.fail("actual byName wrapper was rejected: " + str(exc))
        self.assertEqual(targets, [REMOTE])


class IrohSelectionConditionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="abc-iroh-selection-")
        self.addCleanup(temp.cleanup)
        self.checkout = Path(temp.name).resolve()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("IROH_FORCE_REMOTE_XCFRAMEWORK", None)

    def selected(self, text=None):
        with providers._InputBindings() as bindings:
            return providers.binary_targets(text or manifest(), "IrohLib",
                                            checkout=self.checkout, bindings=bindings)

    def test_remote_selected_when_all_three_actual_local_probes_are_absent(self):
        self.assertEqual(self.selected(), [REMOTE])

    def test_all_actual_local_probe_paths_refuse_unproven_local_variant(self):
        for relative in providers.IROH_LOCAL_BINARIES:
            with self.subTest(relative=relative):
                path = self.checkout / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"unproven local input")
                with self.assertRaisesRegex(ValueError, "selected local Iroh input"):
                    self.selected()
                path.unlink()
        self.assertEqual(self.selected(), [REMOTE])

    def test_force_remote_presence_including_empty_string_disables_local_branch(self):
        path = self.checkout / providers.IROH_LOCAL_BINARIES[0]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"inactive local input")
        for value in ("", "1", "0"):
            with self.subTest(value=value), patch.dict(os.environ, {"IROH_FORCE_REMOTE_XCFRAMEWORK": value}):
                self.assertEqual(self.selected(), [REMOTE])

    def test_nonphysical_local_probe_ancestor_is_not_selection_proof(self):
        (self.checkout / "Iroh.xcframework").symlink_to(self.checkout, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "nonphysical Iroh selection"):
            self.selected()

    def test_conditional_requires_the_exact_pinned_manifest_and_bound_checkout(self):
        with self.assertRaisesRegex(ValueError, "unproven Iroh remote/local"):
            providers.binary_targets(manifest(), "IrohLib")
        with self.assertRaisesRegex(ValueError, "unsupported/unpinned conditional"):
            self.selected(manifest().replace("!forceRemote", "forceRemote"))

    def test_unsupported_conditional_or_external_dependency_forms_stay_rejected(self):
        for dependency in ('.byName(name: "Iroh", condition: .when(platforms: [.macOS]))',
                           '.product(name: "Iroh", package: "other")', 'computedDependency'):
            text = remote_only_manifest().replace('.byName(name: "Iroh")', dependency)
            with self.subTest(dependency=dependency):
                with self.assertRaisesRegex(ValueError, "unsupported selected target dependency"):
                    providers.binary_targets(text, "IrohLib")


if __name__ == "__main__":
    unittest.main()
