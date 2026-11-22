#!/usr/bin/env python3
"""Real directory ABA/source Git; synthetic provider Git evidence and payloads.
No provider requests, compilation or runnable app proof.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import native_cmux as native
import native_cmux_providers as providers
import test_native_cmux as old

REAL_GIT_BLOB = providers.git_blob
REAL_GIT_OUTPUT = providers.git_output
REAL_CHECK_OUTPUT = providers.subprocess.check_output
REAL_FENCE = providers.directory_fence
REAL_FENCES = providers.directory_fences
BINDING_ERROR = r"Directory/ancestor rename, deletion, revoke or event overflow"


@contextmanager
def observe_fences(tracked):
    @contextmanager
    def shared():
        with ExitStack() as observations, REAL_FENCES() as bind:
            @contextmanager
            def fence(path):
                bind(path)
                yield

            yield lambda path: observations.enter_context(tracked(path, fence))

    with patch.object(providers, 'directory_fence', side_effect=tracked), \
            patch.object(providers, 'directory_fences', side_effect=shared):
        yield


class InputBindingTests(unittest.TestCase):
    def fixture(self):
        f = old.BundleTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        base = Path(f.temp.name).resolve()
        cache = base / 'separate-shared-cache'
        cache.mkdir()
        for archive in f.provider_archives:
            archive.rename(cache / ('URL_' + archive.name.replace('.', '_')))
        # Exercise production Git-chain verification with consistent synthetic
        # objects, never a Git subprocess or actual provider distribution.
        objects, revisions = {}, {}

        def obj(kind, raw):
            oid = hashlib.sha1(f'{kind} {len(raw)}\0'.encode() + raw).hexdigest()
            objects[kind, oid] = raw
            return oid

        lockpath = f.provider_source / providers.LOCK
        lock = json.loads(lockpath.read_bytes())
        for pin in lock['pins']:
            name = pin['identity']
            manifest = (f.provider_packages / 'checkouts' / name / 'Package.swift').read_bytes()
            blob = obj('blob', manifest)
            tree = obj('tree', b'100644 Package.swift\0' + bytes.fromhex(blob))
            revision = obj('commit', f'tree {tree}\n\nsynthetic object chain\n'.encode())
            revisions[name] = pin['state']['revision'] = revision
        lockpath.write_text(json.dumps(lock))
        from provider_fixture import pin_source
        pin_source(f, f.provider_source)
        hashes = {str(p.relative_to(f.provider_source)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in f.provider_source.rglob('*') if p.is_file() and '.git' not in p.parts}
        f.synthetic_git_calls = []

        def synthetic_git(command, **kwargs):
            if command[:2] == ['git', '--no-replace-objects']:
                return REAL_CHECK_OUTPUT(command, **kwargs)
            self.assertEqual(command[:2], ['git', '-C'])
            checkout, args = Path(command[2]), command[3:]
            f.synthetic_git_calls.append((checkout, args))
            if checkout == f.provider_source:
                self.assertEqual(args, ['show', '-s', '--format=%H:%T', 'HEAD'])
                return (native.SOURCE_SHA + ':' + native.SOURCE_TREE + '\n').encode()
            if args == ['show', '-s', '--format=%H', 'HEAD']:
                return (revisions[checkout.name] + '\n').encode()
            self.assertEqual(args[0], 'cat-file')
            return objects[args[1], args[2]]

        stack = ExitStack()
        self.addCleanup(stack.close)
        for p in (patch.object(providers, 'git_blob', REAL_GIT_BLOB),
                  patch.object(providers, 'git_output', REAL_GIT_OUTPUT),
                  patch.object(providers, 'source_hashes', return_value=hashes),
                  patch.object(providers.subprocess, 'check_output', side_effect=synthetic_git)):
            stack.enter_context(p)
        return f, cache, base / 'collected.json'

    def aba(self, root):
        before = {str(p.relative_to(root)): native.file_identity(p.stat())
                  for p in root.rglob('*') if p.is_file()}
        held = root.with_name(root.name + '-owned-ABA')
        self.assertFalse(held.exists())
        root.rename(held)
        held.rename(root)
        after = {str(p.relative_to(root)): native.file_identity(p.stat())
                 for p in root.rglob('*') if p.is_file()}
        self.assertTrue(before)
        self.assertEqual(before, after)

    def root(self, f, which, checkout=None):
        return {'source': f.provider_source, 'packages': f.provider_packages,
                'checkouts': f.provider_packages / 'checkouts',
                'checkout': checkout or f.provider_packages / 'checkouts/sparkle',
                'source-nested': (f.provider_source / providers.LOCK).parent}[which]

    def rejection(self, which, operation='collect', last_read=False):
        f, cache, output = self.fixture()
        if operation == 'verify':
            providers.collect(f.provider_source, f.provider_packages, [cache], output)
        moved = []
        original = providers.git_object
        # 9 cat-file reads/derivation (commit/tree/blob x three providers).
        target = 18 if last_read else 1
        reads = []

        def between(*args):
            data = original(*args)
            reads.append(args)
            if len(reads) == target:
                self.aba(self.root(f, which, Path(args[0])))
                moved.append(True)
            return data

        with patch.object(providers, 'git_object', side_effect=between):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                if operation == 'collect':
                    providers.collect(f.provider_source, f.provider_packages, [cache], output)
                elif operation == 'derive':
                    providers.derive(f.provider_source, f.provider_packages, list(cache.iterdir()))
                else:
                    providers.verify(f.app, output)
        self.assertEqual(moved, [True])
        if operation != 'verify':
            self.assertFalse(output.exists(), 'no provider contract after binding failure')

    def test_source_root_between_git_reads_rejected_before_publication(self):
        self.rejection('source')

    def test_packages_root_between_git_reads_rejected_before_publication(self):
        self.rejection('packages')

    def test_checkout_between_git_reads_rejected_before_publication(self):
        self.rejection('checkout')

    def test_checkouts_ancestor_between_git_reads_rejected(self):
        self.rejection('checkouts')

    def test_source_manifest_parent_lifetime_between_git_reads_rejected(self):
        self.rejection('source-nested')

    def test_source_root_last_git_read_second_derivation_rejected(self):
        self.rejection('source', last_read=True)

    def test_packages_root_last_git_read_second_derivation_rejected(self):
        self.rejection('packages', last_read=True)

    def test_checkout_last_git_read_second_derivation_rejected(self):
        self.rejection('checkout', last_read=True)

    def test_standalone_derivation_source_root_rejected(self):
        self.rejection('source', operation='derive')

    def test_standalone_derivation_packages_root_rejected(self):
        self.rejection('packages', operation='derive')

    def test_standalone_derivation_checkout_rejected(self):
        self.rejection('checkout', operation='derive')

    def test_consumer_source_root_rejected(self):
        self.rejection('source', operation='verify')

    def test_consumer_packages_root_rejected(self):
        self.rejection('packages', operation='verify')

    def test_consumer_checkout_rejected(self):
        self.rejection('checkout', operation='verify')

    def test_standalone_git_blob_spans_all_object_reads(self):
        f, cache, output = self.fixture()
        checkout = f.provider_packages / 'checkouts/sparkle'
        revision = json.loads((f.provider_source / providers.LOCK).read_text())['pins'][0]['state']['revision']
        original = providers.git_object
        moved = []

        def between(*args):
            data = original(*args)
            if not moved:
                self.aba(checkout)
                moved.append(True)
            return data

        with patch.object(providers, 'git_object', side_effect=between):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                providers.git_blob(checkout, revision, 'Package.swift')
        self.assertEqual(moved, [True])
        self.assertFalse(output.exists())

    def test_source_binding_retained_between_derivations(self):
        self.phase_gap('source')

    def test_packages_binding_retained_between_derivations(self):
        self.phase_gap('packages')

    def test_checkout_binding_retained_between_derivations(self):
        self.phase_gap('checkout')

    def phase_gap(self, which):
        f, cache, output = self.fixture()
        original = providers.derive
        phases = []

        def between(*args, **kwargs):
            result = original(*args, **kwargs)
            if not phases:
                self.aba(self.root(f, which))
            phases.append(True)
            return result

        with patch.object(providers, 'derive', side_effect=between):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                providers.collect(f.provider_source, f.provider_packages, [cache], output)
        self.assertTrue(phases)
        self.assertFalse(output.exists())

    def test_consumer_retains_source_until_required_payload_sealed(self):
        f, cache, output = self.fixture()
        providers.collect(f.provider_source, f.provider_packages, [cache], output)
        original = providers.read_bound
        moved = []

        def during(path):
            result = original(path)
            # Required app bytes, not ZIP derivation bytes.
            if not moved and Path(path).is_relative_to(f.app):
                self.aba(f.provider_source)
                moved.append(True)
            return result

        with patch.object(providers, 'read_bound', side_effect=during):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                providers.verify(f.app, output)
        self.assertEqual(moved, [True])

    def test_unrelated_global_ancestor_writes_are_accepted(self):
        f, cache, output = self.fixture()
        original = providers.git_object
        # This directory is a WATCHED ancestor of the disjoint input roots.
        # Mutate its own entries (NOTE_WRITE), not a quiet unobserved subtree.
        noise = Path(f.temp.name).resolve()
        moves = []

        def during(*args):
            data = original(*args)
            p = noise / 'unrelated-noise-file'
            p.write_text('benign unrelated child CREATE/WRITE/RENAME/DELETE')
            q = noise / 'unrelated-noise-renamed'
            p.rename(q)
            q.unlink()
            moves.append(True)
            return data

        with patch.object(providers, 'git_object', side_effect=during):
            contract = providers.collect(f.provider_source, f.provider_packages, [cache], output)
            verified = providers.verify(f.app, output)
        self.assertEqual(len(contract['providers']), 3)
        self.assertTrue(verified)
        self.assertEqual(len(moves), 27)
        self.assertTrue(output.exists())

    def test_all_contexts_close_on_success_read_error_and_binding_failure(self):
        f, cache, output = self.fixture()
        active, entered, exited = [], [], []

        @contextmanager
        def tracked(path, fence=REAL_FENCE):
            path = Path(path)
            with fence(path):
                active.append(path)
                entered.append(path)
                try:
                    yield
                finally:
                    active.remove(path)
                    exited.append(path)

        with observe_fences(tracked):
            providers.collect(f.provider_source, f.provider_packages, [cache], output)
            self.assertFalse(active)
            self.assertEqual(len(entered), len(exited))
            with patch.object(providers, 'git_object', side_effect=ValueError('injected read failure')):
                with self.assertRaisesRegex(ValueError, '^injected read failure$'):
                    providers.collect(f.provider_source, f.provider_packages, [cache], output.with_name('error.json'))
            self.assertFalse(active)
            self.assertFalse(output.with_name('error.json').exists())
            original = providers.git_object
            moved = []

            def between(*args):
                data = original(*args)
                if not moved:
                    self.aba(f.provider_source)
                    moved.append(True)
                return data

            with patch.object(providers, 'git_object', side_effect=between):
                with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                    providers.collect(f.provider_source, f.provider_packages, [cache], output.with_name('ABA.json'))
            self.assertFalse(active)
            self.assertEqual(len(entered), len(exited))
            self.assertFalse(output.with_name('ABA.json').exists())

    def test_source_root_fenced_before_cache_discovery(self):
        self.discovery_gap('source')

    def test_packages_root_fenced_before_cache_discovery(self):
        self.discovery_gap('packages')

    def discovery_gap(self, which):
        f, cache, output = self.fixture()
        original = providers.cached_archives
        moved = []

        def during(*args):
            result = original(*args)
            self.aba(self.root(f, which))
            moved.append(True)
            return result

        with patch.object(providers, 'cached_archives', side_effect=during):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                providers.collect(f.provider_source, f.provider_packages, [cache], output)
        self.assertEqual(moved, [True])
        self.assertFalse(output.exists())

    def test_all_selected_checkouts_fenced_before_first_provider_git_read(self):
        f, cache, output = self.fixture()
        last = f.provider_packages / 'checkouts' / list(providers.PRODUCTS)[-1]
        original = providers.git_object
        moved = []

        def during(*args):
            result = original(*args)
            if not moved:
                self.assertNotEqual(Path(args[0]), last)
                self.aba(last)
                moved.append(True)
            return result

        with patch.object(providers, 'git_object', side_effect=during):
            with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                providers.collect(f.provider_source, f.provider_packages, [cache], output)
        self.assertEqual(moved, [True])
        self.assertFalse(output.exists())

    def test_collect_and_verify_reject_real_nonallowlisted_tracked_source(self):
        f, cache, output = self.fixture()
        from provider_fixture import pin_source
        extra = f.provider_source / 'not-in-source-hashes'
        extra.write_bytes(b'pinned nonallowlisted source\n')
        with patch.object(providers.subprocess, 'check_output', REAL_CHECK_OUTPUT):
            pin_source(f, f.provider_source)
        providers.collect(f.provider_source, f.provider_packages, [cache], output)
        REAL_CHECK_OUTPUT(['git', '-C', str(f.provider_source), 'update-index',
                           '--assume-unchanged', extra.name])
        extra.write_bytes(b'changed source hidden by index flags\n')
        with self.assertRaisesRegex(ValueError, 'tracked bytes/target changed'):
            providers.verify(f.app, output)
        output.unlink()
        with self.assertRaisesRegex(ValueError, 'tracked bytes/target changed'):
            providers.collect(f.provider_source, f.provider_packages, [cache], output)
        self.assertFalse(output.exists())

    def test_binding_owner_deduplicates_without_close_rearm_gap(self):
        f, cache, output = self.fixture()
        opened, closed = [], []

        @contextmanager
        def tracked(path, fence=REAL_FENCE):
            with fence(path):
                opened.append(Path(path))
                try:
                    yield
                finally:
                    closed.append(Path(path))

        with observe_fences(tracked):
            with providers._InputBindings() as bindings:
                for _ in range(3):
                    bindings.directory(f.provider_source)
                    bindings.directory(f.provider_packages)
                    bindings.read(f.provider_source / providers.LOCK)
                self.assertEqual(len(opened), 6)  # 3 retained dirs + 3 per-read fences
                self.assertEqual(len(closed), 3)  # only per-read fences closed
                self.aba(f.provider_source)
                with self.assertRaisesRegex(ValueError, BINDING_ERROR):
                    bindings.close()
            self.assertCountEqual(opened, closed)
        self.assertFalse(output.exists())

    def test_partial_binding_acquisition_failure_closes_all_resources(self):
        f, cache, output = self.fixture()
        fd_root = Path('/dev/fd' if os.sys.platform == 'darwin' else '/proc/self/fd')
        before = len(list(fd_root.iterdir()))
        target = f.provider_packages / 'checkouts' / list(providers.PRODUCTS)[1]
        reached = []

        @contextmanager
        def fail_setup(path, fence=REAL_FENCE):
            if Path(path) == target:
                reached.append(True)
                raise OSError('injected watcher acquisition failure')
            with fence(path):
                yield

        with observe_fences(fail_setup):
            with self.assertRaisesRegex(OSError, '^injected watcher acquisition failure$'):
                providers.collect(f.provider_source, f.provider_packages, [cache], output)
        self.assertEqual(reached, [True])
        self.assertEqual(len(list(fd_root.iterdir())), before)
        self.assertFalse(output.exists())

    def test_disjoint_relocated_input_roots_are_not_literal_path_dependent(self):
        f, cache, output = self.fixture()
        base = Path(f.temp.name).resolve()
        new_source = base / 'arbitrary-stock-parent/deep/producer'
        new_packages = base / 'different-package-parent/deep/inputs'
        for original, target in ((f.provider_source, new_source), (f.provider_packages, new_packages)):
            target.parent.mkdir(parents=True)
            original.rename(target)
        f.provider_source, f.provider_packages = new_source, new_packages
        contract = providers.collect(new_source, new_packages, [cache], output)
        self.assertEqual(contract['source'], str(new_source))
        self.assertEqual(contract['packages'], str(new_packages))
        self.assertEqual(len(contract['providers']), 3)
        self.assertTrue(providers.verify(f.app, output))

    def test_actual_fence_descriptors_closed_after_success_and_error(self):
        f, cache, output = self.fixture()
        fd_root = Path('/dev/fd' if os.sys.platform == 'darwin' else '/proc/self/fd')
        before = len(list(fd_root.iterdir()))
        providers.collect(f.provider_source, f.provider_packages, [cache], output)
        providers.verify(f.app, output)
        with patch.object(providers, 'git_object', side_effect=ValueError('injected descriptor test failure')):
            with self.assertRaisesRegex(ValueError, '^injected descriptor test failure$'):
                providers.collect(f.provider_source, f.provider_packages, [cache], output.with_name('fail.json'))
        self.assertEqual(len(list(fd_root.iterdir())), before)
        self.assertFalse(output.with_name('fail.json').exists())


if __name__ == '__main__':
    unittest.main()
