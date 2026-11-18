#!/usr/bin/env python3
"""F1/F2 offline regressions. Real filesystem races and actual ZIP bytes; fake
Mach-O/tool/Git/source fixtures are NEVER genuine native/provider/runtime proof.
"""
import copy
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import native_cmux as native
import native_cmux_providers as providers
from native_cmux_fence import directory_fence
import test_native_cmux as old
from provider_fixture import RESOURCE

REAL_GIT_BLOB = providers.git_blob


def marker(cpu):
    return struct.pack('<IIIIIIII', 0xfeedfacf, cpu, 3, 2, 0, 0, 0, 0) + b'UNIT-ONLY-NOT-EXECUTABLE'


class RepairTests(unittest.TestCase):
    def fixture(self):
        fixture = old.BundleTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def rewrite_reference(self, f):
        f.provider_reference.write_text(json.dumps(providers.derive(f.provider_source, f.provider_packages,
                                                                    f.provider_archives), indent=2))

    def rebuild_distribution(self, f, info_transform=None):
        archive = f.provider_archives[0]
        old_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        with zipfile.ZipFile(archive) as z:
            info = plistlib.loads(z.read('sparkle.xcframework/Info.plist'))
        if info_transform:
            info_transform(info)
        framework = f.app / 'Contents/Frameworks/Sparkle.framework'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('sparkle.xcframework/Info.plist', plistlib.dumps(info))
            prefix = 'sparkle.xcframework/macos-x86_64/Sparkle.framework/'
            for path in sorted(framework.rglob('*')):
                relative = str(path.relative_to(framework))
                zi = zipfile.ZipInfo(prefix + relative + ('/' if path.is_dir() and not path.is_symlink() else ''))
                zi.create_system, zi.external_attr = 3, path.lstat().st_mode << 16
                z.writestr(zi, str(path.readlink()).encode() if path.is_symlink() else path.read_bytes() if path.is_file() else b'')
        new_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest = f.provider_manifests['sparkle'].replace(old_checksum.encode(), new_checksum.encode())
        f.provider_manifests['sparkle'] = manifest
        (f.provider_packages / 'checkouts/sparkle/Package.swift').write_bytes(manifest)

    def nested_fixture(self):
        f = self.fixture()
        prefix = 'Contents/Frameworks/Sparkle.framework/Versions/B/FixtureAgent.xpc/Contents/'
        executable = f.binary(prefix + 'MacOS/fixture-agent')
        executable.write_bytes(marker(0x01000007))
        original_execute = f.execute
        def execute(*args):
            value = original_execute(*args)
            if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == executable:
                value = value.replace(' DYLIB ', ' EXECUTE ')
            return value
        f.execute = execute
        f.plist(prefix + 'Info.plist', 'fixture-agent', '14.0')
        resource = f.app / (prefix + 'Resources/required.dat')
        resource.parent.mkdir()
        resource.write_bytes(b'fictional unit distribution payload, not a real Sparkle member')
        self.rebuild_distribution(f)
        self.rewrite_reference(f)
        return f, [prefix + 'MacOS/fixture-agent', prefix + 'Info.plist', prefix + 'Resources/required.dat',
                   'Contents/Frameworks/Sparkle.framework/' + RESOURCE]

    def directory_aba(self, level):
        f = self.fixture()
        f.main.write_bytes(marker(0x0100000c))
        directory = {'directory': f.main.parent, 'root': f.app, 'ancestor': f.app.parent}[level]
        owned = tempfile.TemporaryDirectory()
        self.addCleanup(owned.cleanup)
        saved, replacement = Path(owned.name) / 'held', Path(owned.name) / 'replacement'
        shutil.copytree(directory, replacement, symlinks=True, copy_function=shutil.copy2)
        alternate = replacement / f.main.relative_to(directory)
        alternate.write_bytes(marker(0x01000007))
        original_metadata = f.main.stat()
        # Replacement leaf metadata is as close as userspace permits. Original
        # file identity, ctime, contents and mtime survive the real rename ABA.
        import os
        os.utime(alternate, ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns))
        original = native.file_identity(f.main.stat())
        active, observed = False, []

        def restore():
            nonlocal active
            if active:
                directory.rename(replacement)
                saved.rename(directory)
                active = False

        def execute(*args):
            nonlocal active
            if args[0] not in ('lipo', 'otool') or Path(args[-1]) != f.main:
                return f.execute(*args)
            if args[0] == 'lipo':
                directory.rename(saved)
                replacement.rename(directory)
                active = True
            cpu = struct.unpack_from('<I', f.main.read_bytes(), 4)[0]
            observed.append(cpu)
            if args[0] == 'lipo':
                return 'x86_64' if cpu == 0x01000007 else 'arm64'
            result = f.execute(*args)
            if args[3] == '-hv':
                restore()
            return result
        try:
            with self.assertRaises(ValueError):
                native.inspect_app(f.app, {}, execute)
        finally:
            restore()
        self.assertEqual(observed, [0x01000007] * 3)
        self.assertEqual(native.file_identity(f.main.stat()), original)
        self.assertEqual(struct.unpack_from('<I', f.main.read_bytes(), 4)[0], 0x0100000c)

    def test_F1_directory_ABA_same_original_leaf_identity(self):
        self.directory_aba('directory')

    def test_F1_bundle_root_ABA_same_original_leaf_identity(self):
        self.directory_aba('root')

    def test_F1_higher_ancestor_ABA_same_original_leaf_identity(self):
        self.directory_aba('ancestor')

    def test_F1_irrelevant_global_temporary_directory_writes_do_not_reject(self):
        f = self.fixture()
        written = False
        def execute(*args):
            nonlocal written
            if not written and args[0] == 'lipo':
                with tempfile.TemporaryDirectory() as sibling:
                    (Path(sibling) / 'irrelevant').write_bytes(b'not an input')
                written = True
            return f.execute(*args)
        report = {}
        native.inspect_app(f.app, report, execute)
        self.assertTrue(report['provider_proof'])
        self.assertTrue(written)

    def test_F1_provider_input_ancestor_ABA_real_read(self):
        f = self.fixture()
        path = f.provider_archives[0]
        directory = path.parent
        owned = tempfile.TemporaryDirectory()
        self.addCleanup(owned.cleanup)
        replacement, saved = Path(owned.name) / 'substitute', Path(owned.name) / 'held'
        shutil.copytree(directory, replacement)
        real_fstat = providers.os.fstat
        calls = 0
        def fstat(fd):
            nonlocal calls
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                calls += 1
                if calls == 1:
                    directory.rename(saved)
                    replacement.rename(directory)
                elif calls == 2:
                    directory.rename(replacement)
                    saved.rename(directory)
            return result
        with patch.object(providers.os, 'fstat', side_effect=fstat):
            with self.assertRaisesRegex(ValueError, 'Directory|pathname'):
                providers.read_bound(path)
        self.assertEqual(calls, 2)

    def test_F2_skeleton_with_absent_distribution_proof_rejects(self):
        f = self.fixture()
        f.provider_reference.unlink()
        (f.app / 'Contents/Frameworks/Sparkle.framework' / RESOURCE).unlink()
        with self.assertRaises(ValueError):
            f.inspect()

    def test_F2_skeleton_with_valid_full_distribution_proof_rejects(self):
        f, members = self.nested_fixture()
        # Remove every nested required payload, keep outer binary and Info.plist.
        shutil.rmtree(f.app / 'Contents/Frameworks/Sparkle.framework/Versions/B/FixtureAgent.xpc')
        (f.app / 'Contents/Frameworks/Sparkle.framework' / RESOURCE).unlink()
        with self.assertRaises(ValueError):
            f.inspect()

    def test_F2_each_required_nested_runtime_member_rejects_if_missing(self):
        # These names are SYNTHETIC source-distribution data, not a hardcoded
        # claim about real provider helpers. The production list comes from ZIP.
        for index in range(4):
            with self.subTest(member=index):
                f, members = self.nested_fixture()
                f.inspect()  # Positive control including host XPC metadata checks.
                (f.app / members[index]).unlink()
                with self.assertRaises((ValueError, OSError)):
                    f.inspect()

    def test_F2_nested_required_helper_arch_minOS_dyld_checks_remain_host_checks(self):
        for defect in ('arch', 'minimum', 'dyld'):
            with self.subTest(defect=defect):
                f, members = self.nested_fixture()
                executable = f.app / members[0]
                if defect == 'arch':
                    f.arch[executable] = 'arm64'
                else:
                    f.loads[executable] = old.commands(minimum='26.0' if defect == 'minimum' else '14.0',
                                                      dependencies=['/usr/lib/not-present.dylib'] if defect == 'dyld' else [])
                with self.assertRaises(ValueError):
                    f.inspect()

    def test_F2_static_providers_and_nonmacOS_slices_are_conditional_not_missing_host_payload(self):
        f = self.fixture()
        proof = f.inspect()['provider_proof']['contract']
        self.assertEqual({p['identity'] for p in proof['providers']}, set(providers.PRODUCTS))
        self.assertEqual([p['identity'] for p in proof['providers'] if p['distribution']['runtimeRequired']], ['sparkle'])
        for provider in proof['providers']:
            self.assertEqual(provider['distribution']['selectedSlice']['SupportedPlatform'], 'macos')
        self.assertFalse((f.app / 'Contents/Frameworks/FixtureStatic.a').exists())
        self.assertFalse((f.app / 'Contents/Frameworks/Sparkle.framework/Headers/Fixture.h').exists())

    def test_F2_wrong_lock_configuration_archive_manifest_or_provider_rejects(self):
        for defect in ('lock', 'config', 'archive', 'manifest', 'provider', 'configuration', 'missing-provider'):
            with self.subTest(defect=defect):
                f = self.fixture()
                if defect == 'lock':
                    path = f.provider_source / providers.LOCK
                    data = json.loads(path.read_text()); data['pins'][0]['state']['revision'] = '0' * 40
                    path.write_text(json.dumps(data))
                elif defect == 'config':
                    (f.provider_source / 'fixture-stock-debug-configuration').write_text('Release')
                elif defect == 'archive':
                    with f.provider_archives[0].open('ab') as out:
                        out.write(b'wrong-checksum')
                elif defect == 'manifest':
                    (f.provider_packages / 'checkouts/sparkle/Package.swift').write_text('wrong working manifest')
                else:
                    data = json.loads(f.provider_reference.read_text())
                    if defect == 'provider':
                        data['providers'][0]['identity'] = 'different-provider'
                    elif defect == 'configuration':
                        data['configuration'] = 'Release'
                    else:
                        data['providers'].pop()
                    f.provider_reference.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    f.inspect()

    def test_F2_wrong_provider_revision_rejects(self):
        f = self.fixture()
        original = providers.git_output
        def wrong(checkout, *args):
            if Path(checkout).name == 'sparkle' and args == ('show', '-s', '--format=%H', 'HEAD'):
                return b'0000000000000000000000000000000000000000\n'
            return original(checkout, *args)
        with patch.object(providers, 'git_output', side_effect=wrong), patch.object(providers, 'git_blob', REAL_GIT_BLOB):
            with self.assertRaisesRegex(ValueError, 'locked revision'):
                f.inspect()

    def test_F2_absent_or_wrong_Intel_macos_distribution_rejects(self):
        for defect in ('platform', 'architecture', 'variant'):
            with self.subTest(defect=defect):
                f = self.fixture()
                def wrong(info):
                    selected = info['AvailableLibraries'][0]
                    if defect == 'platform':
                        selected['SupportedPlatform'] = 'ios'
                    elif defect == 'architecture':
                        selected['SupportedArchitectures'] = ['arm64']
                    else:
                        selected['SupportedPlatformVariant'] = 'maccatalyst'
                self.rebuild_distribution(f, wrong)
                with self.assertRaisesRegex(ValueError, 'Intel macOS'):
                    self.rewrite_reference(f)

    def test_F2_unsupported_unproven_framework_and_dylib_fail_closed(self):
        for relative in ('Contents/Frameworks/Unknown.framework/Versions/A/Unknown',
                         'Contents/Frameworks/unknown-third-party.dylib'):
            with self.subTest(relative=relative):
                f = self.fixture()
                f.binary(relative)
                if '.framework/' in relative:
                    f.plist('Contents/Frameworks/Unknown.framework/Resources/Info.plist', 'Unknown', '14.0')
                    (f.app / 'Contents/Frameworks/Unknown.framework/Unknown').symlink_to('Versions/A/Unknown')
                with patch.object(providers, 'source_owned_images', return_value=set(native.STOCK_PRODUCTS)):
                    with self.assertRaisesRegex(ValueError, 'unsupported/unproven'):
                        f.inspect()

    def test_F2_source_and_provider_physical_identity_remain_bound_at_package(self):
        for target in ('source', 'archive', 'manifest', 'receipt'):
            with self.subTest(target=target):
                f = self.fixture()
                report = f.inspect(); report['ok'] = True
                out = Path(f.temp.name) / 'output'; out.mkdir()
                (out / 'inspect.json').write_text(json.dumps(report))
                path = {'source': f.provider_source / 'fixture-stock-debug-configuration',
                        'archive': f.provider_archives[0],
                        'manifest': f.provider_packages / 'checkouts/sparkle/Package.swift',
                        'receipt': f.provider_reference}[target]
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                path.write_bytes(original)  # Hash restored, ctime/identity must not be.
                with self.assertRaises(ValueError):
                    native.package_app(f.app, out, {})

    def test_F2_provider_contract_not_a_caller_optional_or_allow_missing_list(self):
        f = self.fixture()
        data = json.loads(f.provider_reference.read_text())
        data['providers'][0]['distribution']['requiredMembers'] = {}
        data['allowMissing'] = ['*']
        f.provider_reference.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            f.inspect()

    def test_F2_collector_uses_actual_cache_inputs_and_no_overwrite(self):
        f = self.fixture()
        output = Path(f.temp.name) / 'collected.json'
        contract = providers.collect(f.provider_source, f.provider_packages, [f.provider_archives[0].parent], output)
        self.assertEqual(contract, json.loads(f.provider_reference.read_text()))
        providers.verify(f.app, output)
        with self.assertRaises(FileExistsError):
            providers.collect(f.provider_source, f.provider_packages, [f.provider_archives[0].parent], output)

    def test_F2_unsupported_manifest_expressions_fail_closed(self):
        samples = ['.library(name: "P", targets: ["T"]), .binaryTarget(name: "T", path: "artifact")',
                   '.library(name: "P", targets: ["T"]), .target(name: "T", dependencies: [.product(name: "Other", package: "other")])',
                   '.library(name: "P", targets: computedTargets)']
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises((ValueError, KeyError)):
                    providers.binary_targets(sample, 'P')

    def test_F2_locked_git_object_hash_chain_cannot_be_forged_by_object_filename(self):
        f = self.fixture()
        with patch.object(providers, 'git_output', return_value=b'forged object body'):
            with self.assertRaisesRegex(ValueError, 'object content hash mismatch'):
                providers.git_object(f.provider_packages / 'checkouts/sparkle', 'commit', 'a' * 40)

    def test_F2_runtime_paths_not_blanket_exempted_by_header_directory_name(self):
        self.assertTrue(providers.development_member('Headers/Fixture.h'))
        self.assertTrue(providers.development_member('Versions/B/Modules/module.modulemap'))
        self.assertFalse(providers.development_member('Versions/B/Resources/Headers/RequiredPayload'))
        self.assertFalse(providers.signature_member('Versions/B/_CodeSignature/ExtraHelper'))
        self.assertTrue(providers.signature_member('Versions/B/_CodeSignature/CodeResources'))

    def test_F2_unproven_extra_under_signature_directory_is_not_exempted(self):
        f = self.fixture()
        extra = f.app / 'Contents/Frameworks/Sparkle.framework/Versions/B/_CodeSignature/ExtraPayload'
        extra.parent.mkdir()
        extra.write_bytes(b'not a signature resource')
        with self.assertRaisesRegex(ValueError, 'unproven extra'):
            f.inspect()

    def test_F2_source_identity_changed_during_packaging_rejects_and_removes_archive(self):
        f = self.fixture()
        out = Path(f.temp.name) / 'output'; out.mkdir()
        report = f.inspect(); report['ok'] = True
        (out / 'inspect.json').write_text(json.dumps(report))
        def produce(*args):
            value = old.ReworkTests.fake_ditto(self, *args)
            path = f.provider_source / 'fixture-stock-debug-configuration'
            data = path.read_bytes()
            path.write_bytes(data + b'transient')
            path.write_bytes(data)
            return value
        with patch.object(native, 'run', side_effect=produce):
            with self.assertRaises(ValueError):
                native.package_app(f.app, out, {})
        self.assertFalse((out / 'cmux-build-only.zip').exists())

    def test_F2_malformed_distribution_headers_fail_closed_without_index_errors(self):
        for data in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbe' + struct.pack('>I', 2),
                     struct.pack('<IIIIIIII', 0xfeedfacf, 0x01000007, 3, 6, 1, 0, 0, 0)):
            with self.subTest(data=data.hex()):
                with self.assertRaises(ValueError):
                    providers.macho_content(data)

    def test_F2_absent_stock_archive_cache_fails_closed_without_creating_proof(self):
        f = self.fixture()
        output = Path(f.temp.name) / 'must-not-exist.json'
        with self.assertRaisesRegex(ValueError, 'no cached distribution'):
            providers.collect(f.provider_source, f.provider_packages, [Path(f.temp.name) / 'absent-cache'], output)
        self.assertFalse(output.exists())

    def test_F2_real_git_hash_chain_verification_preserves_exact_manifest_bytes(self):
        # Pure byte objects, not a repository, build, release or real provider pin.
        f = self.fixture()
        def oid(kind, raw):
            return hashlib.sha1(f'{kind} {len(raw)}\0'.encode() + raw).hexdigest()
        blob = b'fixture manifest with exact trailing bytes\n\n'
        blob_id = oid('blob', blob)
        tree = b'100644 Package.swift\0' + bytes.fromhex(blob_id)
        tree_id = oid('tree', tree)
        commit = ('tree ' + tree_id + '\n\nfixture commit\n').encode()
        commit_id = oid('commit', commit)
        objects = {('blob', blob_id): blob, ('tree', tree_id): tree, ('commit', commit_id): commit}
        def output(checkout, *args):
            if args[0] == 'show':
                return (commit_id + '\n').encode()
            self.assertEqual(args[0], 'cat-file')
            return objects[args[1:]]
        with patch.object(providers, 'git_output', side_effect=output):
            self.assertEqual(REAL_GIT_BLOB(f.provider_packages / 'checkouts/sparkle', commit_id, 'Package.swift'), blob)
            objects[('blob', blob_id)] = b'forged manifest'
            with self.assertRaisesRegex(ValueError, 'object content hash mismatch'):
                REAL_GIT_BLOB(f.provider_packages / 'checkouts/sparkle', commit_id, 'Package.swift')

    def test_F2_workflow_requires_collection_before_inspection_and_packaging(self):
        text = (old.ROOT / '.github/workflows/native-cmux-build-only.yml').read_text()
        self.assertLess(text.index('id: providers'), text.index('id: inspect'))
        self.assertLess(text.index('id: inspect'), text.index('id: package'))
        self.assertIn('--providers evidence/providers.json', text)
        self.assertIn('PROVIDERS_OUTCOME: ${{ steps.providers.outcome }}', text)

    def test_F2_Codesign_normalization_binds_code_not_only_architecture(self):
        def signed(payload, signature):
            command = struct.pack('<IIII', 0x1d, 16, 48 + len(payload), len(signature))
            return struct.pack('<IIIIIIII', 0xfeedfacf, 0x01000007, 3, 6, 1, 16, 0, 0) + command + payload + signature
        a, b = signed(b'CODE', b'old-signature'), signed(b'CODE', b'new-signature-more-bytes')
        self.assertEqual(providers.macho_content(a), providers.macho_content(b))
        self.assertNotEqual(providers.macho_content(a), providers.macho_content(signed(b'EVIL', b'old-signature')))


if __name__ == '__main__':
    unittest.main()
