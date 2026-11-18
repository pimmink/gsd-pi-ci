#!/usr/bin/env python3
"""D019 exact-pin C01-C07 regressions. Synthetic bytes/tools only, no native run.

Source: 33daf63d0152d5c7f26155506f0beb191e5ad3da, not guessed helper names.
See source citations in native_cmux.STOCK_PRODUCTS and the private handoff.
"""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_native_cmux as old
import native_cmux as native

PASTE = 'Contents/Resources/bin/cmux-paste-text-worker'
DIFF = 'Contents/Resources/bin/cmux-diff-sidecar'
FFI = 'Contents/Frameworks/libcmux_command_palette_nucleo_ffi.dylib'
PLUGIN = 'Contents/PlugIns/CmuxDockTilePlugin.plugin'
PLUGIN_EXE = PLUGIN + '/Contents/MacOS/CmuxDockTilePlugin'
TUNNEL = 'Contents/Library/SystemExtensions/cmuxTunnel.systemextension'
TUNNEL_EXE = TUNNEL + '/Contents/MacOS/cmuxTunnel'
SPARKLE = 'Contents/Frameworks/Sparkle.framework'
REMOTE = 'Contents/Resources/bin/cmux-tui-ssh/cmux-tui-aarch64-apple-darwin'


def loads(kind, dep):
    return old.commands() + (
        f'Load command 1\n cmd {kind}\n name {dep} (offset 24)\n')


class StockClosureTests(unittest.TestCase):
    def setUp(self):
        self.f = old.BundleTests('test_all_required_binaries_recorded_without_execution')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app = self.f.app
        # The shared original suite now includes the same complete stock roots.
        self.assertTrue((self.app / SPARKLE / 'Versions/Current').is_symlink())

    def execute(self, *args):
        out = self.f.execute(*args)
        return out

    def inspect(self):
        report = {}
        native.inspect_app(self.app, report, self.execute)
        return report

    def image(self, report, relative):
        return next(x for x in report['binaries'] if x['path'] == relative)

    def remote(self):
        p = self.f.binary(REMOTE)
        p.chmod(0o644)
        manifest = p.parent / 'manifest.json'
        manifest.write_text(json.dumps({'commit': 'b' * 40, 'binaries': {p.name: native.sha256(p)}}))
        self.f.arch[p] = 'arm64'
        return p

    def test_C01_each_whole_source_required_product_missing(self):
        # Stock Xcode copy/script phases and explicit dlopen, not filename patterns.
        for relative in (PASTE, DIFF, FFI, PLUGIN, TUNNEL, SPARKLE):
            with self.subTest(product=relative):
                path = self.app / relative
                moved = self.app.parent / 'removed-product'
                path.rename(moved)
                try:
                    with self.assertRaisesRegex(ValueError, 'Missing required'):
                        self.inspect()
                finally:
                    moved.rename(path)

    def test_C02_absent_cmuxd_at_exact_pin_is_not_manufactured(self):
        (self.app / 'Contents/Resources/bin/cmuxd').unlink()
        self.assertTrue(self.inspect()['binaries'])

    def test_C01_dlopen_library_must_be_dylib_without_load_edge(self):
        def execute(*args):
            out = self.execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == self.app / FFI:
                return out.replace('DYLIB', 'EXECUTE')
            return out
        with self.assertRaisesRegex(ValueError, 'MH_DYLIB'):
            native.inspect_app(self.app, {}, execute)

    def test_C03_plugin_type_is_not_application_execute(self):
        def execute(*args):
            out = self.execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == self.app / PLUGIN_EXE:
                return out.replace('BUNDLE', 'EXECUTE')
            return out
        with self.assertRaisesRegex(ValueError, 'MH_BUNDLE'):
            native.inspect_app(self.app, {}, execute)

    def test_C03_systemextension_type_is_execute(self):
        def execute(*args):
            out = self.execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == self.app / TUNNEL_EXE:
                return out.replace('EXECUTE', 'DYLIB')
            return out
        with self.assertRaisesRegex(ValueError, 'MH_EXECUTE'):
            native.inspect_app(self.app, {}, execute)

    def test_C03_plugin_uses_known_Dock_host_not_cmux(self):
        record = self.image(self.inspect(), PLUGIN_EXE)
        self.assertEqual(record['loader_executable'], '/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock')

    def test_C03_plugin_cannot_borrow_cmux_executable_path(self):
        self.f.binary('Contents/Frameworks/cmux-only.dylib')
        self.f.loads[self.app / PLUGIN_EXE] = old.commands(dependencies=['@executable_path/../Frameworks/cmux-only.dylib'])
        with self.assertRaises(ValueError):
            self.inspect()

    def test_C03_plugin_cannot_borrow_main_rpath(self):
        self.f.binary('Contents/Frameworks/cmux-only.dylib')
        self.f.loads[self.f.main] = old.commands(rpaths=['@executable_path/../Frameworks'])
        self.f.loads[self.app / PLUGIN_EXE] = old.commands(dependencies=['@rpath/cmux-only.dylib'])
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            self.inspect()

    def test_C03_missing_plugin_or_extension_plist_fails(self):
        for relative in (PLUGIN, TUNNEL):
            p = self.app / relative / 'Contents/Info.plist'
            data = p.read_bytes()
            p.unlink()
            try:
                with self.assertRaisesRegex(ValueError, 'Info.plist'):
                    self.inspect()
            finally:
                p.write_bytes(data)

    def chain(self):
        a = self.f.binary('Contents/Frameworks/A.dylib')
        b = self.f.binary('Contents/Frameworks/Deep/B.dylib')
        c = self.f.binary('Contents/Frameworks/Deep/C.dylib')
        self.f.loads[self.f.main] = old.commands(rpaths=['@executable_path/../Frameworks'], dependencies=['@rpath/A.dylib'])
        self.f.loads[a] = old.commands(rpaths=['@loader_path/Deep'], dependencies=['@rpath/B.dylib'])
        self.f.loads[b] = old.commands(dependencies=['@rpath/C.dylib'])
        return a, b, c

    def test_C04_intermediate_loader_runpath_stack(self):
        _, _, c = self.chain()
        report = self.inspect()
        b = self.image(report, 'Contents/Frameworks/Deep/B.dylib')
        self.assertIn(str(c.relative_to(self.app)), str(b['resolved_dependencies']))

    def test_C04_missing_transitive_strong_dep_fails(self):
        _, _, c = self.chain()
        c.unlink()
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            self.inspect()

    def test_C04_cycles_terminate_and_keep_edges(self):
        a, b, _ = self.chain()
        self.f.loads[b] = old.commands(dependencies=['@rpath/A.dylib'])
        report = self.inspect()
        self.assertIn('A.dylib', str(self.image(report, str(b.relative_to(self.app)))['resolved_dependencies']))

    def test_C04_graph_depth_bound_fails_closed(self):
        self.chain()
        with patch.object(native, 'MAX_DYLD_DEPTH', 1, create=True):
            with self.assertRaisesRegex(ValueError, 'depth'):
                self.inspect()

    def test_C04_first_existing_runpath_candidate_wrong_type_no_fallback(self):
        a = self.f.binary('Contents/Frameworks/First/A.dylib')
        self.f.binary('Contents/Frameworks/Second/A.dylib')
        a.write_bytes(b'not Mach-O')
        self.f.loads[self.f.main] = old.commands(rpaths=['@loader_path/../Frameworks/First', '@loader_path/../Frameworks/Second'], dependencies=['@rpath/A.dylib'])
        with self.assertRaisesRegex(ValueError, 'non-Mach-O'):
            self.inspect()

    def test_C05_absent_safe_weak_dep_distinct_unproven_result(self):
        self.f.loads[self.f.main] = loads('LC_LOAD_WEAK_DYLIB', '@rpath/Optional.dylib')
        record = self.image(self.inspect(), 'Contents/MacOS/cmux')
        self.assertIn('weak-absent', str(record['resolved_dependencies']))

    def test_C05_absent_weak_Apple_dep_not_mandatory(self):
        self.f.loads[self.f.main] = loads('LC_LOAD_WEAK_DYLIB', '/System/Library/Frameworks/New.framework/New')
        self.assertIn('weak-absent', str(self.inspect()))

    def test_C05_weak_unsafe_absolute_path_still_fails(self):
        self.f.loads[self.f.main] = loads('LC_LOAD_WEAK_DYLIB', '/opt/homebrew/lib/Optional.dylib')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_C05_strong_reexport_upward_lazy_missing_all_fail(self):
        for kind in ('LC_LOAD_DYLIB', 'LC_REEXPORT_DYLIB', 'LC_LOAD_UPWARD_DYLIB', 'LC_LAZY_LOAD_DYLIB'):
            with self.subTest(kind=kind):
                self.f.loads[self.f.main] = loads(kind, '@loader_path/Missing.dylib')
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_C05_SDK26_metadata_never_claims_host15_runtime(self):
        report = self.inspect()
        self.assertEqual(report.get('runtime_compatibility'), 'UNPROVEN')
        self.assertEqual(report['target_host'], '15.7.9')
        self.assertIn('static', report.get('compatibility_scope', '').lower())

    def test_x86_slice_minimum_not_contaminated_by_other_arch(self):
        text = old.commands('14.0') + old.commands('26.0').replace('architecture x86_64', 'architecture arm64')
        self.assertEqual(native.load_commands(text)['minimum_os'], '14.0')

    def test_x86_26_rejected_even_when_arm_minimum14(self):
        text = old.commands('26.0') + old.commands('14.0').replace('architecture x86_64', 'architecture arm64')
        with self.assertRaises(ValueError):
            native.load_commands(text)

    def test_C06_exact_manifest_bound_arm_remote_data(self):
        self.remote()
        report = self.inspect()
        self.assertIn(REMOTE, {x['path'] for x in report.get('remote_payload_data', [])})
        self.assertNotIn(REMOTE, {x['path'] for x in report['binaries']})

    def test_C06_no_remote_exemption_without_matching_digest(self):
        p = self.remote()
        p.write_bytes(p.read_bytes() + b'mutation')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_C06_remote_exemption_not_extension_or_prefix_blanket(self):
        p = self.f.binary('Contents/Resources/bin/cmux-tui-ssh/arbitrary-arm64.dylib')
        p.chmod(0o644)
        self.f.arch[p] = 'arm64'
        with self.assertRaisesRegex(ValueError, 'x86_64'):
            self.inspect()

    def test_C06_host_edge_to_remote_promotes_it(self):
        self.remote()
        self.f.loads[self.f.main] = old.commands(dependencies=['@executable_path/../Resources/bin/cmux-tui-ssh/cmux-tui-aarch64-apple-darwin'])
        with self.assertRaisesRegex(ValueError, 'x86_64'):
            self.inspect()

    def test_C06_arm_local_dlopen_library_never_remote(self):
        self.f.arch[self.app / FFI] = 'arm64'
        with self.assertRaisesRegex(ValueError, 'x86_64'):
            self.inspect()

    def test_internal_symlink_external_bounce_is_not_safe(self):
        outside = self.app.parent / 'outside'
        outside.mkdir()
        (outside / 'back').symlink_to(self.app / 'Contents')
        (self.app / 'bounce').symlink_to('../outside/back')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_rpath_double_slash_cannot_turn_into_absolute_system_path(self):
        self.f.loads[self.f.main] = old.commands(rpaths=['@loader_path/../Frameworks'], dependencies=['@rpath//usr/lib/libSystem.B.dylib'])
        with self.assertRaises(ValueError):
            self.inspect()

    def test_wrong_cpu_in_requested_x86_header_fails(self):
        def execute(*args):
            out = self.execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv'):
                return out.replace('X86_64', 'ARM64')
            return out
        with self.assertRaisesRegex(ValueError, 'x86_64'):
            native.inspect_app(self.app, {}, execute)

    def test_plist_x86_override_not_other_arch_or_generic_fallback(self):
        p = self.app / 'Contents/Info.plist'
        data = old.plistlib.loads(p.read_bytes())
        data['LSMinimumSystemVersion'] = '26.0'
        data['LSMinimumSystemVersionByArchitecture'] = {'x86_64': '14.0', 'arm64': '26.0'}
        p.write_bytes(old.plistlib.dumps(data))
        self.assertTrue(self.inspect()['binaries'])

    def test_all_required_host_images_get_arch_and_host_ceiling_checks(self):
        for relative in (PASTE, DIFF, FFI, PLUGIN_EXE, TUNNEL_EXE, SPARKLE + '/Versions/B/Sparkle'):
            p = self.app / relative
            for invalid in ('arm-only', '15.7.10'):
                with self.subTest(path=relative, invalid=invalid):
                    if invalid == 'arm-only':
                        self.f.arch[p] = 'arm64'
                    else:
                        self.f.loads[p] = old.commands(invalid)
                    try:
                        with self.assertRaises(ValueError):
                            self.inspect()
                    finally:
                        self.f.arch.pop(p, None)
                        self.f.loads.pop(p, None)

    def test_internal_versions_current_topology_survives_packaging(self):
        report = self.inspect()
        report['ok'] = True
        out = self.app.parent / 'evidence'
        out.mkdir()
        (out / 'inspect.json').write_text(json.dumps(report))
        with patch.object(native, 'run', side_effect=lambda *args: old.ReworkTests.fake_ditto(self, *args)):
            native.package_app(self.app, out, {})
        with old.zipfile.ZipFile(out / 'cmux-build-only.zip') as archive:
            self.assertEqual(archive.read(self.app.name + '/' + SPARKLE + '/Versions/Current'), b'B')

    def test_present_weak_wrong_type_not_ignored(self):
        self.f.loads[self.f.main] = loads('LC_LOAD_WEAK_DYLIB', '@loader_path/../Resources/bin/ghostty')
        with self.assertRaisesRegex(ValueError, 'MH_DYLIB'):
            self.inspect()

    def test_source_product_producers_and_provider_lock_are_hash_bound(self):
        expected = {
            'scripts/build-app-bundled-resources.sh': '3a32dc6be520e811f232ec3f3b7d6822fb16aae73b053ba01f70416ea6805378',
            'scripts/build-diff-sidecar.sh': '12cbcb12b78edfe2f3e8fe9361074fc73fc0b6e754aa15d284550a315a694b6e',
            'scripts/build-command-palette-nucleo-ffi.sh': '2928ee6d05f845cf7c30a9b7380c74e179d714727ba29458deae5692411f0325',
            'cmux.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved': 'e85b10d8e60e9c87adef48b32a1cfd2a5f89008371cd2f67b370c245e7ad5082',
        }
        for path, digest in expected.items():
            self.assertEqual(native.SOURCE_FILES.get(path), digest, path)

    def test_remote_x86_dylib_host_edge_revokes_DATA_receipt(self):
        p = self.remote()
        self.f.arch[p] = 'x86_64'
        self.f.loads[self.f.main] = old.commands(dependencies=['@executable_path/../Resources/bin/cmux-tui-ssh/' + p.name])
        def execute(*args):
            out = self.execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == p:
                return out.replace('EXECUTE', 'DYLIB')
            return out
        report = {}
        native.inspect_app(self.app, report, execute)
        self.assertIn(REMOTE, {x['path'] for x in report['binaries']})
        self.assertNotIn(REMOTE, {x['path'] for x in report['remote_payload_data']})

    def test_dyld_context_and_edge_bounds_fail_closed(self):
        for constant in ('MAX_DYLD_CONTEXTS', 'MAX_DYLD_EDGES'):
            with self.subTest(bound=constant), patch.object(native, constant, 1):
                with self.assertRaisesRegex(ValueError, 'bound'):
                    self.inspect()

    def test_inventory_entry_limit_is_bounded_while_enumerating(self):
        with patch.object(Path, 'rglob', return_value=(self.f.main for _ in range(40001))):
            with self.assertRaisesRegex(ValueError, 'file count'):
                native.bundle_paths(self.app)

    def test_cyclic_symlink_fails_without_escaping_exception(self):
        (self.app / 'cycle-a').symlink_to('cycle-b')
        (self.app / 'cycle-b').symlink_to('cycle-a')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.inspect()

    def test_universal_plist_arm_minimum_does_not_contaminate_x86(self):
        p = self.app / 'Contents/Info.plist'
        data = old.plistlib.loads(p.read_bytes())
        data['LSMinimumSystemVersionByArchitecture'] = {'x86_64': '15.7.9', 'arm64': '26.0'}
        p.write_bytes(old.plistlib.dumps(data))
        self.assertEqual(self.inspect()['target_host'], '15.7.9')

    def test_tagged_outer_app_keeps_InfoPlist_main_executable(self):
        self.f.main.rename(self.f.main.with_name('cmux DEV'))
        self.f.plist('Contents/Info.plist', 'cmux DEV', '14.0')
        self.assertEqual(self.inspect()['main_executable'], 'Contents/MacOS/cmux DEV')

    def test_transient_inspection_image_swap_restored_before_snapshot_is_refused(self):
        main = self.f.main
        original = main.stat()
        original_bytes = main.read_bytes()
        swapped = False
        def execute(*args):
            nonlocal swapped
            if args[0] == 'lipo' and Path(args[-1]) == main and not swapped:
                swapped = True
                # The tool observed different bytes, then the original inode,
                # bytes/mode/mtime were restored. Only the change identity remains.
                main.write_bytes(original_bytes + b'transient different image')
                observed = 'x86_64'
                main.write_bytes(original_bytes)
                old.os.utime(main, ns=(original.st_atime_ns, original.st_mtime_ns))
                return observed
            return self.execute(*args)
        with self.assertRaisesRegex(ValueError, 'Bundle changed during inspection'):
            native.inspect_app(self.app, {}, execute)
        self.assertTrue(swapped)

    def test_untracked_cmuxd_producer_cannot_enable_stock_condition(self):
        f = old.D015RecipeTests('test_prepare_retains_exact_source_and_final_path')
        f.setUp()
        self.addCleanup(f.doCleanups)
        (f.source / 'cmuxd').mkdir()
        with self.assertRaisesRegex(ValueError, 'cmuxd producer'):
            f.prepare()

    def test_C07_receipt_does_not_claim_launch_free_recipe(self):
        text = (old.ROOT / 'scripts/native_cmux.py').read_text()
        self.assertNotIn('recipe="stock tagged reload; no build-only; no launch;', text)
        text = (old.ROOT / '.github/workflows/native-cmux-build-only.yml').read_text()
        self.assertNotIn('no-auto-launch review', text)


if __name__ == '__main__':
    unittest.main()
