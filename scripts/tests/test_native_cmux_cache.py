#!/usr/bin/env python3
"""D024 F2-R1: real filesystem/cache-key discovery, synthetic provider ZIPs.
No network, build, genuine release/provider compatibility or runtime claim.
All prior 170 tests and their method bodies remain untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import native_cmux_providers as providers
import test_native_cmux as old


class CacheDiscoveryTests(unittest.TestCase):
    def fixture(self, names=None):
        f = old.BundleTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        base = Path(f.temp.name).resolve()
        cache = base / 'Library/Caches/org.swift.swiftpm/artifacts'
        cache.mkdir(parents=True)
        originals = list(f.provider_archives)
        archives = []
        for index, archive in enumerate(originals):
            name = names[index] if names else 'https___example_invalid_' + archive.name.replace('.', '_')
            target = cache / name
            archive.rename(target)
            archives.append(target)
        self.assertTrue(all(not p.exists() for p in originals))
        self.assertFalse(list(f.provider_packages.rglob('*.zip')))
        output = base / 'collected-from-shared-cache.json'
        return f, cache, archives, output

    def collect(self, f, cache, output):
        return providers.collect(f.provider_source, f.provider_packages,
                                 [f.provider_packages / 'artifacts', cache], output)

    def rejected(self, f, cache, output, pattern=None):
        with self.assertRaises((ValueError, OSError)) as caught:
            self.collect(f, cache, output)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        self.assertFalse(output.exists(), 'Failed collection must not publish provider proof')

    def test_URL_mangled_shared_cache_only_without_retained_workspace_ZIP(self):
        f, cache, archives, output = self.fixture()
        # Workspace extraction is NOT distribution authority and has no ZIP.
        extracted = f.provider_packages / 'artifacts/provider/Extracted.framework'
        extracted.mkdir(parents=True)
        (extracted / 'NotDistributionProof').write_bytes(b'extracted workspace inventory is irrelevant')
        expected = {hashlib.sha256(p.read_bytes()).hexdigest() for p in archives}
        contract = self.collect(f, cache, output)
        self.assertEqual(len(contract['providers']), 3)
        self.assertEqual({p['archive']['sha256'] for p in contract['providers']}, expected)
        self.assertEqual({p['archive']['path'] for p in contract['providers']}, {str(p) for p in archives})
        self.assertEqual(contract, json.loads(output.read_text()))
        providers.verify(f.app, output)

    def test_extensionless_arbitrary_names_use_ZIP_bytes_not_filename(self):
        f, cache, archives, output = self.fixture(['opaque-cache-key', 'artifact.data', 'UPPERCASE_ZIP'])
        archives[0].rename(cache / 'unrelated-prefix')
        self.assertEqual(len(self.collect(f, cache, output)['providers']), 3)

    def test_non_ZIP_siblings_do_not_become_archive_candidates(self):
        f, cache, archives, output = self.fixture()
        (cache / 'not-an-archive.zip').write_bytes(b'not a ZIP')
        (cache / ('0' * 64)).write_bytes(b'not a ZIP either')
        nested = cache / 'nested'
        nested.mkdir()
        archives[0].rename(nested / 'nested_URL_cache_key_zip')
        self.assertEqual(len(self.collect(f, cache, output)['providers']), 3)

    def test_wrong_checksum_valid_ZIP_is_not_provider_authority(self):
        f, cache, archives, output = self.fixture()
        archives[0].write_bytes(archives[0].read_bytes() + b'checksum mismatch')
        self.assertTrue(zipfile.is_zipfile(archives[0]))
        self.rejected(f, cache, output, 'checksum-verified')

    def test_non_ZIP_cache_only_fails_without_emitting_contract(self):
        f, cache, archives, output = self.fixture()
        for archive in archives:
            archive.write_bytes(b'ordinary non-ZIP cache data')
        self.rejected(f, cache, output, 'no cached distribution archives')

    def test_symlink_cache_root_is_not_trusted(self):
        f, cache, archives, output = self.fixture(['a.zip', 'b.zip', 'c.zip'])
        alias = cache.parent / 'alias'
        alias.symlink_to(cache, target_is_directory=True)
        self.rejected(f, alias, output)

    def test_symlink_cache_ancestor_is_not_trusted(self):
        f, cache, archives, output = self.fixture(['a.zip', 'b.zip', 'c.zip'])
        alias = cache.parents[1] / 'ancestor-alias'
        alias.symlink_to(cache.parent, target_is_directory=True)
        self.rejected(f, alias / cache.name, output)

    def test_symlink_leaf_cannot_supply_missing_distribution(self):
        f, cache, archives, output = self.fixture()
        outside = cache.parent / 'outside'
        outside.mkdir()
        for archive in archives:
            target = outside / archive.name
            archive.rename(target)
            archive.symlink_to(target)
        self.rejected(f, cache, output)

    def test_symlink_subdirectory_cannot_supply_distribution(self):
        f, cache, archives, output = self.fixture()
        outside = cache.parent / 'outside'
        cache.rename(outside)
        cache.mkdir()
        (cache / 'linked').symlink_to(outside, target_is_directory=True)
        self.rejected(f, cache, output)

    def test_hardlinked_archive_is_rejected(self):
        f, cache, archives, output = self.fixture()
        os.link(archives[0], cache.parent / 'hardlink')
        self.rejected(f, cache, output, 'single-link')

    def test_archive_replacement_between_discovery_and_derivation_is_rejected(self):
        f, cache, archives, output = self.fixture(['a.zip', 'b.zip', 'c.zip'])
        derive = providers.derive
        replaced = False

        def swap(*args):
            nonlocal replaced
            if not replaced:
                replaced = True
                replacement = cache / 'replacement'
                replacement.write_bytes(archives[0].read_bytes())
                replacement.replace(archives[0])
            return derive(*args)

        with patch.object(providers, 'derive', side_effect=swap):
            self.rejected(f, cache, output)
        self.assertTrue(replaced)

    def test_cache_ancestor_ABA_during_derivation_is_rejected(self):
        f, cache, archives, output = self.fixture(['a.zip', 'b.zip', 'c.zip'])
        derive = providers.derive
        moved = False

        def aba(*args):
            nonlocal moved
            result = derive(*args)
            if not moved:
                moved = True
                alternate = cache.parent / 'moved-and-restored'
                cache.rename(alternate)
                alternate.rename(cache)
            return result

        with patch.object(providers, 'derive', side_effect=aba):
            self.rejected(f, cache, output)
        self.assertTrue(moved)

    def test_archive_replacement_during_ZIP_probe_is_rejected(self):
        f, cache, archives, output = self.fixture(['a.zip', 'b.zip', 'c.zip'])
        is_zipfile = zipfile.is_zipfile
        replaced = False

        def probe(stream):
            nonlocal replaced
            result = is_zipfile(stream)
            if result and not replaced:
                replaced = True
                replacement = cache / 'replacement'
                replacement.write_bytes(archives[0].read_bytes())
                replacement.replace(archives[0])
            return result

        with patch.object(providers.zipfile, 'is_zipfile', side_effect=probe):
            self.rejected(f, cache, output)
        self.assertTrue(replaced)

    def test_archive_count_limit_is_not_bypassed_by_URL_cache_keys(self):
        f, cache, archives, output = self.fixture()
        data = archives[0].read_bytes()
        for index in range(254):
            (cache / f'URL_cache_{index}_zip').write_bytes(data)
        self.rejected(f, cache, output, 'discovery bound')

    def test_archive_size_bound_remains_mandatory(self):
        f, cache, archives, output = self.fixture()
        with patch.object(providers, 'MAX_BYTES', 1):
            self.rejected(f, cache, output, 'bound')

    def test_archive_total_byte_bound_remains_mandatory(self):
        f, cache, archives, output = self.fixture()
        data = max((p.read_bytes() for p in archives), key=len)
        for index in range(5):
            (cache / f'extra_URL_{index}_zip').write_bytes(data)
        with patch.object(providers, 'MAX_BYTES', len(data)):
            self.rejected(f, cache, output, 'byte bound')

    def test_provider_manifest_Git_binding_remains_mandatory(self):
        f, cache, archives, output = self.fixture()
        manifest = next((f.provider_packages / 'checkouts').glob('*/Package.swift'))
        manifest.write_bytes(manifest.read_bytes() + b'// uncommitted physical manifest mutation\n')
        self.rejected(f, cache, output, 'manifest')


if __name__ == '__main__':
    unittest.main()
