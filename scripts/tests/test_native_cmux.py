#!/usr/bin/env python3
"""Offline fixtures only: no native build, app launch, API call, or cancellation."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import os
import shutil
import stat
import zipfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_cmux as native
import native_cmux_queue as queue

ROOT = Path(__file__).resolve().parents[2]
CREATED = "2026-11-17T16:00:00Z"
WORKFLOW_SHA = "a" * 40
TAG = "d014-exclusive-20261117-1600"
RUN_ID = 987654321


def commands(minimum="14.0", rpaths=(), dependencies=(), legacy=False):
    out = "fixture (architecture x86_64):\n"
    blocks = [f"cmd LC_VERSION_MIN_MACOSX\n version {minimum}" if legacy else
              f"cmd LC_BUILD_VERSION\n platform 1\n minos {minimum}\n sdk 26.6"]
    blocks += [f"cmd LC_RPATH\n path {p} (offset 12)" for p in rpaths]
    blocks += [f"cmd LC_LOAD_DYLIB\n name {d} (offset 24)" for d in dependencies]
    for i, block in enumerate(blocks):
        out += f"Load command {i}\n {block}\n"
    return out


def run_fixture():
    return {"id": RUN_ID, "repository": {"full_name": native.CI_REPO},
            "head_repository": {"full_name": native.CI_REPO}, "head_sha": WORKFLOW_SHA,
            "path": native.WORKFLOW, "created_at": CREATED, "event": "workflow_dispatch",
            "run_attempt": 1, "display_title": queue.title(TAG), "status": "in_progress"}


def job_fixture(started=False):
    return {"id": 88, "run_id": RUN_ID, "run_attempt": 1, "name": queue.NATIVE_JOB,
            "labels": ["macos-26-intel"], "status": "in_progress" if started else "queued",
            "runner_id": 42 if started else 0,
            # Deliberately populated even while queued: this must not defeat the queue cap.
            "started_at": CREATED, "steps": [{"name": "Set up job", "status": "completed", "started_at": CREATED}] if started else []}


class FakeAPI:
    def __init__(self):
        self.run = run_fixture()
        self.jobs = [job_fixture()]
        self.cancels = []
        self.observations = 0
        self.before_cancel_started = False
        self.cancel_error = False
        self.approval_error = False
        self.network_error = False
        self.start_in_cancel = False

    def snapshot(self, run_id):
        assert run_id == RUN_ID
        self.observations += 1
        if self.network_error:
            raise RuntimeError("network unknown")
        if self.before_cancel_started and self.observations == 3:
            self.jobs = [job_fixture(True)]
        return deepcopy(self.run), deepcopy(self.jobs)

    def approvals(self, run_id):
        assert run_id == RUN_ID
        if self.approval_error:
            raise RuntimeError("approval API unknown")
        return []

    def cancel(self, run_id):
        self.cancels.append(run_id)
        if self.start_in_cancel:
            self.jobs = [job_fixture(True)]
        if self.cancel_error:
            raise RuntimeError("POST outcome unknown")
        return {"http_status": 202}


class ParserTests(unittest.TestCase):
    def test_x64_and_fat(self):
        for value in ("x86_64", "x86_64 arm64", "Architectures in the fat file: fixture are: x86_64 arm64",
                      "Non-fat file: fixture is architecture: x86_64"):
            self.assertIn("x86_64", native.architectures(value))

    def test_arm_only_missing_and_unparseable(self):
        for value in ("arm64", "", "x86_64 ???", "build success", "x86_64h"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.architectures(value)

    def test_macho_filetype_header(self):
        header = 'fixture:\nMach header\n magic cputype cpusubtype caps filetype ncmds sizeofcmds flags\n MH_MAGIC_64 X86_64 ALL 0x00 EXECUTE 1 32 FLAGS\n'
        self.assertEqual(native.file_type(header), 'EXECUTE')
        self.assertEqual(native.file_type(header.replace('EXECUTE', 'DYLIB')), 'DYLIB')
        for malformed in ('', header.replace('EXECUTE', 'OBJECT'), 'magic filetype\n short'):
            with self.assertRaises(ValueError):
                native.file_type(malformed)

    def test_build_and_legacy_minimums(self):
        for legacy in (False, True):
            self.assertEqual(native.load_commands(commands("15.7.9", legacy=legacy))["minimum_os"], "15.7.9")
        self.assertEqual(native.load_commands(commands())["minimum_os"], "14.0")

    def test_bad_minimums_platform_and_commands(self):
        for value in (commands("26.0"), commands("15.7.10"), commands("bad"), "", "Load command 0\n cmd LC_UUID\n",
                      commands().replace("platform 1", "platform 2"), commands().replace("minos 14.0", "missing 14.0"),
                      commands() + commands(), "Load command 0\n invalid\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.load_commands(value)

    def test_rpath_and_dependency_parse(self):
        data = native.load_commands(commands(rpaths=["@executable_path/../Frameworks"], dependencies=["@rpath/Example.framework/Example"]))
        self.assertEqual(data["dependencies"], ["@rpath/Example.framework/Example"])
        with self.assertRaises(ValueError):
            native.load_commands(commands(rpaths=["@loader_path"]).replace(" (offset 12)", ""))
        with self.assertRaises(ValueError):
            native.load_commands(commands() + "Load command 9\n cmd LC_DYLD_ENVIRONMENT\n")

    def test_versions(self):
        for value in (None, 14, "14.0 beta", "14.0.0.1", "-1", ""):
            with self.assertRaises(ValueError):
                native.version(value)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name).resolve() / "fixture.app"
        self.main = self.binary("Contents/MacOS/cmux")
        self.plist("Contents/Info.plist", "cmux", "14.0")
        self.binary("Contents/Resources/bin/ghostty")
        self.binary("Contents/Resources/bin/cmux-tui")
        # Keep distinct original helpers and their assertions; add all stock roots.
        # cmuxd is optional at this pin, but presence still exercises host checks.
        for helper in ('cmux', 'cmuxd'):
            (self.app / 'Contents/Resources/bin' / helper).symlink_to('cmux-tui')
        self.binary("Contents/Library/cmux Computer Use.app/Contents/MacOS/cmux-cua")
        self.plist("Contents/Library/cmux Computer Use.app/Contents/Info.plist", "cmux-cua", "14.0")
        self.binary('Contents/Resources/bin/cmux-paste-text-worker')
        self.binary('Contents/Resources/bin/cmux-diff-sidecar')
        self.binary('Contents/Frameworks/libcmux_command_palette_nucleo_ffi.dylib')
        self.binary('Contents/PlugIns/CmuxDockTilePlugin.plugin/Contents/MacOS/CmuxDockTilePlugin')
        self.plist('Contents/PlugIns/CmuxDockTilePlugin.plugin/Contents/Info.plist', 'CmuxDockTilePlugin', '14.0')
        self.binary('Contents/Library/SystemExtensions/cmuxTunnel.systemextension/Contents/MacOS/cmuxTunnel')
        self.plist('Contents/Library/SystemExtensions/cmuxTunnel.systemextension/Contents/Info.plist', 'cmuxTunnel', '14.0')
        self.binary('Contents/Frameworks/Sparkle.framework/Versions/B/Sparkle')
        self.plist('Contents/Frameworks/Sparkle.framework/Versions/B/Resources/Info.plist', 'Sparkle', '14.0')
        framework = self.app / 'Contents/Frameworks/Sparkle.framework'
        (framework / 'Versions/Current').symlink_to('B')
        (framework / 'Resources').symlink_to('Versions/Current/Resources')
        (framework / 'Sparkle').symlink_to('Versions/Current/Sparkle')
        self.arch = {}
        self.loads = {}
        self.sdk = Path(self.temp.name).resolve() / 'MacOSX.sdk'
        self.stub('usr/lib/libSystem.B.tbd')
        # Required F2 inputs: synthetic provider/source/ZIP fixture, never real
        # release evidence. No existing test method/assertion changes.
        from provider_fixture import install
        install(self)

    def stub(self, relative):
        p = self.sdk / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('--- !tapi-tbd\ntargets: [ x86_64-macos, arm64-macos ]\n')
        return p

    def binary(self, relative):
        p = self.app / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes.fromhex("cffaedfe") + b"offline fixture; never executed")
        p.chmod(0o755)
        return p

    def plist(self, relative, executable, minimum):
        p = self.app / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(plistlib.dumps({"CFBundleExecutable": executable, "LSMinimumSystemVersion": minimum,
                                        **({'NSDockTilePlugIn': 'CmuxDockTilePlugin.plugin'} if relative == 'Contents/Info.plist' else {})}))
        return p

    def execute(self, *args):
        if args == ('xcrun', '--sdk', 'macosx', '--show-sdk-path'):
            return str(self.sdk)
        p = Path(args[-1])
        if args[0] == "lipo":
            return self.arch.get(p, "x86_64 arm64")
        if args[:4] == ('otool', '-arch', 'x86_64', '-hv'):
            kind = ('BUNDLE' if '.plugin/' in str(p) else
                    'DYLIB' if p.suffix == '.dylib' or '.framework' in str(p) else 'EXECUTE')
            return f'magic cputype cpusubtype caps filetype ncmds sizeofcmds flags\nMH_MAGIC_64 X86_64 ALL 0x00 {kind} 1 32 FLAGS\n'
        self.assertEqual(args[:4], ("otool", "-arch", "x86_64", "-l"))
        return self.loads.get(p, commands(dependencies=["/usr/lib/libSystem.B.dylib"]))

    def inspect(self):
        report = {}
        native.inspect_app(self.app, report, self.execute)
        return report

    def test_all_required_binaries_recorded_without_execution(self):
        report = self.inspect()
        self.assertEqual(len(report["binaries"]), 10)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in report["binaries"]))
        self.assertEqual(report["target_host"], "15.7.9")

    def test_arm_only_embedded_helper_fails(self):
        self.arch[self.app / "Contents/Resources/bin/ghostty"] = "arm64"
        with self.assertRaisesRegex(ValueError, "x86_64"):
            self.inspect()

    def test_missing_main_and_source_required_helper(self):
        self.main.unlink()
        with self.assertRaisesRegex(ValueError, "Missing required embedded executable"):
            self.inspect()
        self.binary("Contents/MacOS/cmux")
        (self.app / "Contents/Resources/bin/ghostty").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required source-defined helper"):
            self.inspect()

    def test_missing_embedded_framework_binary(self):
        self.plist("Contents/Frameworks/Foo.framework/Versions/A/Resources/Info.plist", "Foo", "14.0")
        framework = self.app / "Contents/Frameworks/Foo.framework"
        (framework / "Resources").symlink_to("Versions/A/Resources")
        with self.assertRaisesRegex(ValueError, "Missing required embedded executable"):
            self.inspect()

    def test_plist_or_embedded_binary_minimum_too_new(self):
        self.plist("Contents/Info.plist", "cmux", "26.0")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.inspect()
        self.plist("Contents/Info.plist", "cmux", "14.0")
        self.loads[self.app / "Contents/Resources/bin/cmux-tui"] = commands("26.0")
        with self.assertRaisesRegex(ValueError, "minOS"):
            self.inspect()

    def test_bundled_dependency_and_rpath(self):
        lib = self.binary("Contents/Frameworks/libExample.dylib")
        self.loads[self.main] = commands(rpaths=["@executable_path/../Frameworks"], dependencies=["@rpath/libExample.dylib"])
        self.assertEqual(len(self.inspect()["binaries"]), 11)
        lib.unlink()
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            self.inspect()

    def test_swift_system_rpath_cannot_hide_missing_framework_or_library(self):
        self.loads[self.main] = commands(rpaths=['/usr/lib/swift'], dependencies=['@rpath/Missing.framework/Missing'])
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            self.inspect()
        self.loads[self.main] = commands(rpaths=['/usr/lib/swift'], dependencies=['@rpath/libswiftCore.dylib'])
        self.stub('usr/lib/swift/libswiftCore.tbd')
        self.inspect()
        self.loads[self.main] = commands(dependencies=['/usr/lib/nonexistent.dylib'])
        with self.assertRaisesRegex(ValueError, 'Missing SDK'):
            self.inspect()
        stub = self.sdk / 'usr/lib/libSystem.B.tbd'
        stub.write_text('targets: [ arm64-macos ]')
        self.loads[self.main] = commands(dependencies=['/usr/lib/libSystem.B.dylib'])
        with self.assertRaisesRegex(ValueError, 'x86_64 macOS'):
            self.inspect()

    def test_forbidden_developer_and_escaping_rpaths(self):
        for path in ("/Users/developer/DerivedData/Frameworks", "/opt/homebrew/lib", "@loader_path/../../../../outside", "/Applications/Xcode.app/Contents/Developer/Library"):
            self.loads[self.main] = commands(rpaths=[path])
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.inspect()

    def test_forbidden_absolute_dependency_and_missing_direct_dependency(self):
        for dep in ("/usr/local/lib/libbad.dylib", "@loader_path/missing.dylib", "/System/Library/../bad.dylib"):
            self.loads[self.main] = commands(dependencies=[dep])
            with self.assertRaises(ValueError):
                self.inspect()

    def test_malformed_plist_and_nonexecutable_main(self):
        info = self.app / 'Contents/Info.plist'
        info.write_bytes(plistlib.dumps(['not a dictionary']))
        with self.assertRaisesRegex(ValueError, 'dictionary'):
            self.inspect()
        self.plist('Contents/Info.plist', 'cmux', '14.0')
        self.main.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'execute bit'):
            self.inspect()

    def test_external_symlink(self):
        (self.app / "outside").symlink_to(self.temp.name)
        with self.assertRaisesRegex(ValueError, "External"):
            self.inspect()

    def test_flat_helper_uses_own_executable_anchor(self):
        helper = self.app / 'Contents/Resources/bin/ghostty'
        self.binary('Contents/Resources/lib/libFlat.dylib')
        self.loads[helper] = commands(rpaths=['@executable_path/../lib'], dependencies=['@rpath/libFlat.dylib'])
        record = next(b for b in self.inspect()['binaries'] if b['path'].endswith('/ghostty'))
        self.assertEqual(record['loader_executable'], 'Contents/Resources/bin/ghostty')

    def test_nested_helper_uses_own_executable_rpaths(self):
        helper = self.app / "Contents/Library/cmux Computer Use.app/Contents/MacOS/cmux-cua"
        self.binary("Contents/Library/cmux Computer Use.app/Contents/Frameworks/libHelper.dylib")
        self.loads[helper] = commands(rpaths=["@executable_path/../Frameworks"], dependencies=["@rpath/libHelper.dylib"])
        report = self.inspect()
        record = next(b for b in report["binaries"] if b["path"].endswith("cmux-cua"))
        self.assertTrue(record["loader_executable"].endswith("cmux-cua"))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.api = FakeAPI()
        self.now = queue.timestamp(CREATED)

    def watch(self, **kwargs):
        def sleep(seconds):
            self.now += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            return queue.watch(self.api, RUN_ID, WORKFLOW_SHA, CREATED, TAG, self.out,
                               clock=lambda: self.now, sleep=kwargs.get("sleep", sleep))

    def result(self):
        return json.loads((self.out / "result.json").read_text())

    def test_current_identity(self):
        queue.validate_run(run_fixture(), RUN_ID, WORKFLOW_SHA, CREATED, TAG)

    def test_wrong_repo_id_time_head_workflow_source_attempt(self):
        mutations = {"id": 111, "repository": {"full_name": "pimmink/cmux"}, "head_repository": {"full_name": "other/repo"},
                     "head_sha": native.SOURCE_SHA, "created_at": "2026-11-13T06:42:02Z", "path": "other.yml",
                     "display_title": "wrong source or tag", "run_attempt": 2, "event": "push"}
        for key, value in mutations.items():
            run = run_fixture()
            run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                queue.validate_run(run, RUN_ID, WORKFLOW_SHA, CREATED, TAG)

    def test_aux_started_native_queued_placeholder_is_not_start(self):
        aux = job_fixture(True)
        aux["name"] = "exact-run queue watchdog (not native start)"
        self.assertEqual(queue.native_state([aux, job_fixture()], RUN_ID, CREATED), "not-started")
        self.assertEqual(queue.native_state([job_fixture(True)], RUN_ID, CREATED), "started")
        allocated_but_queued = job_fixture(True)
        allocated_but_queued['status'] = 'queued'
        self.assertEqual(queue.native_state([allocated_but_queued], RUN_ID, CREATED), 'not-started')
        placeholder = job_fixture(True)
        placeholder["steps"] = []
        self.assertEqual(queue.native_state([placeholder], RUN_ID, CREATED), "not-started")

    def test_wrong_native_job_or_labels(self):
        for key, value in (("run_id", 2), ("run_attempt", 2), ("labels", ["macos-26"]), ("started_at", "2026-11-13T07:54:02Z")):
            job = job_fixture(True)
            job[key] = value
            with self.assertRaises(ValueError):
                queue.native_state([job], RUN_ID, CREATED)
        with self.assertRaises(ValueError):
            queue.native_state([job_fixture(), job_fixture()], RUN_ID, CREATED)

    def test_deadline_only_exact_new_run_cancelled_once_after_diagnosis(self):
        self.assertEqual(self.watch(), 2)
        self.assertEqual(self.api.cancels, [RUN_ID])
        self.assertLessEqual(self.now, queue.timestamp(CREATED) + queue.QUEUE_SECONDS)
        self.assertTrue((self.out / "deadline-diagnosis.json").exists())
        self.assertTrue((self.out / "cancel-intent.json").exists())
        self.assertIn("ONE fallback", self.result()["parent_action"])
        self.assertNotEqual(RUN_ID, 43380511172)

    def test_actually_started_no_cancel(self):
        self.api.jobs = [job_fixture(True)]
        self.assertEqual(self.watch(), 0)
        self.assertEqual(self.api.cancels, [])

    def test_no_historical_late_or_future_attachment(self):
        for delta in (-10, queue.QUEUE_SECONDS, 4 * 86400):
            self.now = queue.timestamp(CREATED) + delta
            with self.assertRaises(ValueError):
                self.watch()
        self.assertEqual(self.api.cancels, [])
        self.assertEqual(self.api.observations, 0)

    def test_wrong_identity_never_cancels(self):
        self.api.run["head_sha"] = native.SOURCE_SHA
        self.assertEqual(self.watch(), 2)
        self.assertEqual(self.api.cancels, [])

    def test_approval_unknown_preserved_before_exact_cancel(self):
        self.api.approval_error = True
        self.watch()
        diagnosis = json.loads((self.out / "deadline-diagnosis.json").read_text())
        self.assertIn("approval_error", diagnosis)
        self.assertEqual(self.api.cancels, [RUN_ID])

    def test_network_or_cancel_unknown_not_retried(self):
        self.api.cancel_error = True
        self.watch()
        self.assertEqual(self.api.cancels, [RUN_ID])
        self.assertIn("unknown", self.result()["outcome"])

    def test_network_before_identity_no_cancel(self):
        self.api.network_error = True
        self.watch()
        self.assertEqual(self.api.cancels, [])
        self.assertEqual(self.api.observations, 1)

    def test_native_starts_after_cancel_post_preserves_race(self):
        self.api.start_in_cancel = True
        self.watch()
        self.assertEqual(self.api.cancels, [RUN_ID])
        self.assertEqual(self.result()['after_cancel_native_state'], 'started')
        self.assertTrue(self.result()['needs_parent'])
        self.assertNotIn('confirmed', self.result()['outcome'].replace('not-confirmed', ''))

    def test_late_start_never_claims_queue_bound_success(self):
        def sleep(_):
            self.now = queue.timestamp(CREATED) + queue.QUEUE_SECONDS + 1
            job = job_fixture(True)
            # job.started_at deliberately retains the original queued placeholder.
            job['steps'][0]['started_at'] = '2026-11-17T16:15:01Z'
            self.api.jobs = [job]
        self.assertEqual(self.watch(sleep=sleep), 2)
        self.assertEqual(self.result()['outcome'], 'native-start-after-deadline')
        self.assertEqual(self.api.cancels, [])

    def test_native_starts_during_diagnosis_not_cancelled(self):
        self.now += queue.QUEUE_SECONDS - queue.CANCEL_MARGIN - 1
        self.api.before_cancel_started = True
        self.watch(sleep=lambda _: setattr(self, "now", self.now + 1))
        self.assertEqual(self.api.cancels, [])

    def test_terminal_run_not_cancelled(self):
        self.api.run["status"] = "completed"
        self.watch()
        self.assertEqual(self.api.cancels, [])


class PackagingTests(unittest.TestCase):
    def test_partial_or_oversized_new_archive_never_uploaded(self):
        for failure in ('partial', 'oversized', 'changed'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                app = root / 'fixture.app'
                app.mkdir()
                binary = app / 'fixture'
                binary.write_bytes(b'fixture only')
                out = root / 'evidence'
                out.mkdir()
                snapshot = native.bundle_snapshot(app)
                inspection = {'ok': True, 'inventory_schema': 1, 'bundle_identity': snapshot['identity'],
                              'inventory': snapshot['inventory'],
                              'binaries': [{'path': 'fixture', 'sha256': native.sha256(binary)}], 'plists': []}
                (out / 'inspect.json').write_text(json.dumps(inspection))
                if failure == 'changed':
                    binary.write_bytes(b'changed')
                def ditto(*args):
                    self.assertEqual(args[0], 'ditto')
                    Path(args[-1]).write_bytes(b'partial fixture')
                    if failure == 'partial':
                        raise ValueError('tool failed')
                    return ''
                with patch.object(sys, 'argv', ['native_cmux.py', 'package', '--app', str(app), '--output', str(out)]), \
                     patch.object(native, 'run', side_effect=ditto), patch.object(native, 'MAX_UPLOAD', 64), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(native.main(), 1)
                self.assertFalse((out / 'cmux-build-only.zip').exists())
                self.assertFalse(json.loads((out / 'package.json').read_text())['ok'])

    def test_prior_unknown_archive_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / 'inspect.json').write_text(json.dumps({'ok': True, 'binaries': [], 'plists': []}))
            archive = out / 'cmux-build-only.zip'
            archive.write_bytes(b'preserve unknown prior output')
            with patch.object(sys, 'argv', ['native_cmux.py', 'package', '--app', temp, '--output', temp]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(native.main(), 1)
            self.assertEqual(archive.read_bytes(), b'preserve unknown prior output')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name).resolve()
        (self.source / '.xcode-version').write_text('26\n')
        (self.source / 'cmux.xcodeproj').mkdir()
        self.project = self.source / 'cmux.xcodeproj/project.pbxproj'
        self.project.write_text('MACOSX_DEPLOYMENT_TARGET = 14.0;\n')
        from provider_fixture import pin_source
        pin_source(self, self.source)
        self.answers = {
            ('uname', '-m'): 'x86_64', ('sw_vers', '-productVersion'): '26.0',
            ('xcodebuild', '-version'): 'Xcode 26.6\nBuild version 17F113',
            ('xcrun', '--sdk', 'macosx', '--show-sdk-version'): '26.6',
            ('rustc', '-vV'): 'rustc fixture\nhost: x86_64-apple-darwin',
            ('git', 'rev-parse', 'HEAD^{tree}'): native.SOURCE_TREE,
            ('bun', '--version'): native.BUN_VERSION,
            ('git', 'status', '--porcelain', '--untracked-files=no'): '',
            ('git', 'submodule', 'status', '--recursive'): ' ' + 'b' * 40 + ' ghostty (fixture)',
        }
        self.env = {'GITHUB_REPOSITORY': native.CI_REPO, 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_SHA': WORKFLOW_SHA, 'EXPECTED_WORKFLOW_SHA': WORKFLOW_SHA}

    def fake_run(self, *args, cwd=None):
        if args == ('git', 'rev-parse', 'HEAD'):
            return native.SOURCE_SHA if cwd == self.source else WORKFLOW_SHA
        return self.answers.get(args, 'fixture')

    def preflight(self, **kwargs):
        report = {}
        with patch.object(native.sys, 'platform', kwargs.get('platform', 'darwin')), \
             patch.object(native, 'run', side_effect=self.fake_run), \
             patch.object(native.shutil, 'which', side_effect=lambda name: None if name == kwargs.get('missing') else '/fixture/' + name), \
             patch.object(native, 'check_native_absence'), \
             patch.dict(native.os.environ, self.env, clear=True):
            native.preflight(self.source, TAG, report)
        return report

    def test_preflight_positive_records_distinct_source_and_workflow(self):
        report = self.preflight()
        self.assertEqual(report['source_head'], native.SOURCE_SHA)
        self.assertEqual(report['workflow_sha'], WORKFLOW_SHA)
        self.assertEqual(report['runner_arch'], 'x86_64')
        self.assertEqual(report['deployment_target_declarations'], ['14.0'])
        self.assertEqual(report['metal'], 'fixture')

    def test_fail_before_compile_for_incompatible_host_or_toolchain(self):
        with self.assertRaises(ValueError):
            self.preflight(platform='linux')
        for args, bad in ((('uname', '-m'), 'arm64'), (('xcodebuild', '-version'), 'Xcode 25.0'),
                          (('xcrun', '--sdk', 'macosx', '--show-sdk-version'), '15.0'),
                          (('rustc', '-vV'), 'host: aarch64-apple-darwin')):
            with self.subTest(args=args):
                original = self.answers[args]
                self.answers[args] = bad
                with self.assertRaises(ValueError):
                    self.preflight()
                self.answers[args] = original

    def test_preflight_does_not_consult_index_or_submodule_status(self):
        self.answers[('git', 'status', '--porcelain', '--untracked-files=no')] = ' M index-only'
        self.answers[('git', 'submodule', 'status', '--recursive')] = '-not-source-byte-authority'
        self.assertIn('source_inventory', self.preflight())

    def test_missing_global_prerequisite_and_override(self):
        with self.assertRaisesRegex(ValueError, 'Missing expected prerequisite zig'):
            self.preflight(missing='zig')
        self.env['CMUX_SKIP_ZIG_BUILD'] = '1'
        with self.assertRaisesRegex(ValueError, 'Unexpected CMUX'):
            self.preflight()

    def test_source_minos_and_arm_route_rejected_before_compile(self):
        from provider_fixture import pin_source
        self.project.write_text('MACOSX_DEPLOYMENT_TARGET = 26.0;\n')
        pin_source(self, self.source)
        with self.assertRaisesRegex(ValueError, 'deployment target'):
            self.preflight()
        self.project.write_text('MACOSX_DEPLOYMENT_TARGET = 14.0;\n ARCHS = arm64;\n')
        pin_source(self, self.source)
        with self.assertRaisesRegex(ValueError, 'arm-only'):
            self.preflight()

    def test_wrong_workflow_sha_attempt_tag_or_repo(self):
        for key, bad in (('GITHUB_SHA', native.SOURCE_SHA), ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_REPOSITORY', 'pimmink/cmux')):
            original = self.env[key]
            self.env[key] = bad
            with self.assertRaises(ValueError):
                self.preflight()
            self.env[key] = original
        for tag in ('shared', 'd014-;echo bad', '../default', 'd014-short'):
            self.assertFalse(native.tag_ok(tag))


class WorkflowTests(unittest.TestCase):
    def test_yaml_and_build_safety_contract(self):
        # Ruby/Psych is already on the local macOS host; no pip/npm dependency installation.
        text = subprocess.check_output(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(ROOT / native.WORKFLOW)], text=True)
        wf = json.loads(text)
        self.assertEqual(list(wf["on"]), ["workflow_dispatch"])
        self.assertEqual(wf["permissions"], {"contents": "read"})
        jobs = wf["jobs"]
        self.assertEqual(set(jobs), {"queue-guard", "native-build"})
        build = jobs["native-build"]
        self.assertEqual(build["runs-on"], "macos-26-intel")
        self.assertNotIn("strategy", build)
        self.assertFalse(build["concurrency"]["cancel-in-progress"])
        steps = build["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertEqual(commands.count("./scripts/reload.sh"), 1)
        self.assertIn('CMUX_DEV_BACKEND_MODE=local ./scripts/reload.sh --tag "$BUILD_TAG" --no-global-cli-links --derived-data "$DERIVED_DATA"', commands)
        self.assertIn("github.run_attempt == '1'", build["if"])
        self.assertIn("--tag", commands)
        for step in steps:
            if "actions/checkout@" in step.get("uses", ""):
                self.assertFalse(step["with"]["persist-credentials"])
            if "actions/upload-artifact@" in step.get("uses", ""):
                self.assertEqual(step["with"]["retention-days"], 1)
        forbidden = ("workflow run", "--launch", "--app-name", "brew install", "setup.sh", "release create", "force-cancel", "blacksmith")
        for term in forbidden:
            self.assertNotIn(term, commands.lower())
        self.assertEqual(jobs["queue-guard"]["permissions"]["actions"], "read")
        self.assertNotIn("needs", jobs["queue-guard"])

    def test_cancel_transport_targets_only_fixed_repo_and_exact_id(self):
        api = queue.GitHub('fixture-token-never-sent')
        with patch.object(api, 'request', return_value={'http_status': 202}) as request:
            api.cancel(RUN_ID)
        request.assert_called_once_with('POST', f'actions/runs/{RUN_ID}/cancel')
        self.assertEqual(native.CI_REPO, 'pimmink/gsd-pi-ci')

    def test_guard_has_no_dispatch_or_retry_surface(self):
        self.assertFalse(hasattr(queue.GitHub, "dispatch"))
        self.assertFalse(hasattr(queue.GitHub, "retry"))
        self.assertEqual(native.TARGET_HOST, "15.7.9")
        self.assertEqual(native.MAX_UPLOAD, 400 * 1024**2)




class ReworkTests(unittest.TestCase):
    """F2-F5 regressions: synthetic bytes, mocked Apple/archive/API calls only."""
    def setUp(self):
        self.fixture = BundleTests('test_all_required_binaries_recorded_without_execution')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        self.out = self.app.parent / 'evidence'
        self.out.mkdir()
        self.resource = self.app / 'Contents/Resources/plain.txt'
        self.resource.write_text('also inspected resources')
        report = self.fixture.inspect()
        report['ok'] = True
        (self.out / 'inspect.json').write_text(json.dumps(report))

    def fake_ditto(self, *args):
        self.assertEqual(args[0], 'ditto')
        app, archive = map(Path, args[-2:])
        with zipfile.ZipFile(archive, 'w') as z:
            for p in [app] + sorted(app.rglob('*')):
                name = str(p.relative_to(app.parent))
                mode = p.lstat().st_mode
                if p.is_dir() and not p.is_symlink():
                    name += '/'
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = mode << 16
                z.writestr(entry, os.readlink(p).encode() if p.is_symlink() else b'' if p.is_dir() else p.read_bytes())
        return ''

    def package(self, execute=None, app=None):
        with patch.object(sys, 'argv', ['native_cmux.py', 'package', '--app', str(app or self.app), '--output', str(self.out)]), \
             patch.object(native, 'run', side_effect=execute or self.fake_ditto), contextlib.redirect_stdout(io.StringIO()):
            return native.main()

    def test_f2_same_bytes_external_symlink_refused(self):
        outside = self.app.parent / 'outside'
        outside.write_bytes(self.fixture.main.read_bytes())
        self.fixture.main.unlink()
        self.fixture.main.symlink_to(outside)
        self.assertEqual(self.package(), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f2_new_uninspected_image_refused(self):
        self.fixture.binary('Contents/Resources/new-image')
        self.assertEqual(self.package(), 1)

    def test_f2_resource_and_link_inventory_refused(self):
        self.resource.write_text('changed non-Mach-O resource')
        self.assertEqual(self.package(), 1)

    def test_f2_exact_bundle_identity_not_just_equal_content(self):
        other = self.app.parent / 'other.app'
        shutil.copytree(self.app, other, symlinks=True)
        self.assertEqual(self.package(app=other), 1)

    def test_f2_private_copy_and_archive_verified(self):
        inputs = []
        def archive(*args):
            inputs.append(Path(args[-2]))
            return self.fake_ditto(*args)
        self.assertEqual(self.package(archive), 0)
        self.assertNotEqual(inputs, [self.app])
        self.assertEqual(json.loads((self.out / 'package.json').read_text()).get('archive_verified'), True)

    def test_f2_archive_injection_refused_and_removed(self):
        def inject(*args):
            self.fake_ditto(*args)
            with zipfile.ZipFile(args[-1], 'a') as z:
                z.writestr(self.app.name + '/Contents/MacOS/uninspected', b'uninspected')
            return ''
        self.assertEqual(self.package(inject), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f2_copy_symlinks_stay_confined_and_unchanged(self):
        link = self.app / 'Contents/Resources/alias'
        link.symlink_to('plain.txt')
        report = self.fixture.inspect()
        report['ok'] = True
        (self.out / 'inspect.json').write_text(json.dumps(report))
        self.assertEqual(self.package(), 0)
        with zipfile.ZipFile(self.out / 'cmux-build-only.zip') as z:
            entry = z.getinfo(self.app.name + '/Contents/Resources/alias')
            self.assertTrue(stat.S_ISLNK(entry.external_attr >> 16))
            self.assertEqual(z.read(entry), b'plain.txt')

    def test_f3_all_dylib_load_kinds_reject_execute_target(self):
        for kind in ('LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB', 'LC_REEXPORT_DYLIB',
                     'LC_LOAD_UPWARD_DYLIB', 'LC_LAZY_LOAD_DYLIB'):
            self.fixture.loads[self.fixture.main] = commands(dependencies=['@loader_path/cmux']).replace('LC_LOAD_DYLIB', kind)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'DYLIB'):
                self.fixture.inspect()

    def test_f3_rpath_cannot_skip_wrong_type_for_later_good_library(self):
        wrong = self.fixture.binary('Contents/Wrong/libExample.dylib')
        self.fixture.binary('Contents/Frameworks/libExample.dylib')
        self.fixture.loads[self.fixture.main] = commands(rpaths=['@executable_path/../Wrong', '@executable_path/../Frameworks'], dependencies=['@rpath/libExample.dylib'])
        execute = self.fixture.execute
        def header(*args):
            value = execute(*args)
            return value.replace(' DYLIB ', ' EXECUTE ') if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == wrong else value
        with self.assertRaisesRegex(ValueError, 'DYLIB'):
            native.inspect_app(self.app, {}, header)

    def test_f3_preserves_actual_load_kind(self):
        data = native.load_commands(commands(dependencies=['/usr/lib/libSystem.B.dylib']).replace('LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB'))
        self.assertEqual(data.get('dylib_loads'), [{'command': 'LC_LOAD_WEAK_DYLIB', 'name': '/usr/lib/libSystem.B.dylib'}])

    def test_f4_documented_internal_and_parent_observers_cancel_once_total(self):
        api = FakeAPI()
        wf = (ROOT / native.WORKFLOW).read_text()
        for role in ('internal', 'parent'):
            now = [queue.timestamp(CREATED)]
            def sleep(seconds):
                now[0] += seconds
            out = self.out / role
            kwargs = {'observe_only': True} if role == 'internal' and '--observe-only' in wf else {}
            with contextlib.redirect_stdout(io.StringIO()):
                queue.watch(api, RUN_ID, WORKFLOW_SHA, CREATED, TAG, out, clock=lambda: now[0], sleep=sleep, **kwargs)
        self.assertEqual(api.cancels, [RUN_ID])

    def test_f4_heartbeat_and_no_internal_write_permission(self):
        wf = (ROOT / native.WORKFLOW).read_text()
        guard = wf.split('  queue-guard:', 1)[1].split('  native-build:', 1)[0]
        self.assertIn('actions: read', guard)
        self.assertNotIn('actions: write', guard)
        self.assertIn('--observe-only', guard)
        api = FakeAPI()
        api.jobs = [job_fixture(True)]
        with contextlib.redirect_stdout(io.StringIO()):
            queue.watch(api, RUN_ID, WORKFLOW_SHA, CREATED, TAG, self.out, clock=lambda: queue.timestamp(CREATED))
        self.assertTrue((self.out / 'heartbeat.json').exists())

    def test_f5_always_capture_real_native_log_and_stdout(self):
        wf = json.loads(subprocess.check_output(['ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))', str(ROOT / native.WORKFLOW)], text=True))
        steps = wf['jobs']['native-build']['steps']
        build = next(s for s in steps if s.get('id') == 'build')
        self.assertIn('capture', build['run'])
        diagnostic = next((s for s in steps if s.get('id') == 'diagnostics'), {})
        self.assertEqual(diagnostic.get('if'), 'always()')
        self.assertIn('diagnostics', diagnostic.get('run', ''))

    def test_f5_native_log_tail_is_bounded_redacted_and_attributed(self):
        collect = getattr(native, 'collect_build_logs', None)
        self.assertTrue(callable(collect), 'missing real native-log artifact collector')
        log = self.app.parent / f'cmux-reload-{TAG}.log'
        secret = 'ghp_' + 'X' * 40
        log.write_bytes(b'A' * (3 * 1024**2) + ('\nAuthorization: Bearer ' + secret + '\n').encode())
        with patch.dict(os.environ, {'BUILD_TAG': TAG, 'GITHUB_RUN_ID': str(RUN_ID), 'GITHUB_SHA': WORKFLOW_SHA}):
            collect(self.out, TAG, temp_root=log.parent)
        retained = self.out / 'native-build.log'
        self.assertLessEqual(retained.stat().st_size, 2 * 1024**2)
        self.assertNotIn(secret, retained.read_text())
        meta = json.loads((self.out / 'build-logs.json').read_text())
        self.assertEqual(meta['expected_source_sha'], native.SOURCE_SHA)
        self.assertTrue(meta['native']['truncated'])
        self.assertEqual(meta['native']['original_bytes_observed'], log.stat().st_size)
        self.assertEqual(meta['native']['sha256'], native.sha256(retained))

    def test_f5_stdout_capture_bounded_and_total_upload_enforced(self):
        capture = getattr(native, 'capture_build_stdout', None)
        finalize = getattr(native, 'finalize_evidence', None)
        self.assertTrue(callable(capture) and callable(finalize), 'missing bounded stdout/final artifact gate')
        capture(io.BytesIO(b'B' * (3 * 1024**2)), self.out)
        self.assertLessEqual((self.out / 'build-stdout.log').stat().st_size, 2 * 1024**2)
        (self.out / 'cmux-build-only.zip').write_bytes(b'oversize')
        with patch.object(native, 'MAX_UPLOAD', 1024), self.assertRaises(ValueError):
            finalize(self.out)
        self.assertEqual((self.out / 'cmux-build-only.zip').read_bytes(), b'oversize')

    def test_f2_same_bytes_symlink_swap_during_copy_is_never_followed(self):
        outside = self.app.parent / 'same-bytes-outside'
        data = self.fixture.main.read_bytes()
        outside.write_bytes(data)
        original = shutil.copyfile
        external_reads = []
        def swap(src, dst, *args, **kwargs):
            if Path(src) != self.fixture.main:
                return original(src, dst, *args, **kwargs)
            self.fixture.main.unlink()
            self.fixture.main.symlink_to(outside)
            try:
                result = original(src, dst, *args, **kwargs)
                external_reads.append(True)
                return result
            finally:
                self.fixture.main.unlink()
                self.fixture.main.write_bytes(data)
                self.fixture.main.chmod(0o755)
        with patch.object(native.shutil, 'copyfile', side_effect=swap):
            self.assertEqual(self.package(), 0)
        self.assertEqual(external_reads, [], 'copy followed an external same-byte replacement')

    def test_f2_copy_nofollow_rejects_file_and_parent_swap(self):
        for component in ('cmux', 'MacOS'):
            with self.subTest(component=component):
                original = os.open
                source = self.fixture.main if component == 'cmux' else self.fixture.main.parent
                saved = source.with_name(source.name + '.saved')
                outside = self.app.parent / ('external-' + component)
                if component == 'cmux':
                    outside.write_bytes(source.read_bytes())
                else:
                    outside.mkdir()
                    (outside / 'cmux').write_bytes(self.fixture.main.read_bytes())
                intercepted = []
                def swap(path, flags, *args, **kwargs):
                    if path != component or 'dir_fd' not in kwargs:
                        return original(path, flags, *args, **kwargs)
                    intercepted.append(flags)
                    source.rename(saved)
                    source.symlink_to(outside)
                    try:
                        return original(path, flags, *args, **kwargs)
                    finally:
                        source.unlink()
                        saved.rename(source)
                with patch.object(native.os, 'open', side_effect=swap):
                    self.assertEqual(self.package(), 1)
                self.assertTrue(intercepted)
                self.assertTrue(all(flags & os.O_NOFOLLOW for flags in intercepted))
                self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f2_directory_mode_and_symlink_inventory_mutations_fail(self):
        target = self.app / 'Contents/Resources'
        target.chmod(0o700 if stat.S_IMODE(target.stat().st_mode) != 0o700 else 0o755)
        self.assertEqual(self.package(), 1)

    def test_f2_mutation_during_private_copy_and_archive_bytes_fail(self):
        original = native.copy_inspected_bundle
        def mutate(*args, **kwargs):
            value = original(*args, **kwargs)
            self.resource.write_text('mutated while copying')
            return value
        with patch.object(native, 'copy_inspected_bundle', side_effect=mutate):
            self.assertEqual(self.package(), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f2_wrong_archive_file_bytes_and_modes_fail(self):
        def corrupt(*args):
            staged = Path(args[-2])
            (staged / 'Contents/Resources/plain.txt').write_text('archive producer changed a resource')
            return self.fake_ditto(*args)
        self.assertEqual(self.package(corrupt), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f2_valid_archive_over_total_budget_refused(self):
        with patch.object(native, 'MAX_UPLOAD', 64):
            self.assertEqual(self.package(), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_f3_dylib_load_rejects_mh_bundle_target(self):
        target = self.fixture.binary('Contents/Frameworks/libExample.dylib')
        self.fixture.loads[self.fixture.main] = commands(dependencies=['@loader_path/../Frameworks/libExample.dylib'])
        execute = self.fixture.execute
        def header(*args):
            value = execute(*args)
            return value.replace(' DYLIB ', ' BUNDLE ') if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == target else value
        with self.assertRaisesRegex(ValueError, 'MH_DYLIB'):
            native.inspect_app(self.app, {}, header)

    def test_f5_absent_or_external_inner_log_is_never_fabricated(self):
        native.collect_build_logs(self.out, TAG, temp_root=self.app.parent)
        meta = json.loads((self.out / 'build-logs.json').read_text())
        self.assertEqual(meta['native']['state'], 'absent')
        external = self.app.parent / 'external.log'
        external.write_bytes(b'outside')
        (self.app.parent / f'cmux-reload-{TAG}.log').symlink_to(external)
        with self.assertRaises(OSError):
            native.collect_build_logs(self.out, TAG, temp_root=self.app.parent)

    def test_f5_failed_producer_keeps_real_exit_and_stdout_evidence(self):
        script = 'set -o pipefail; (printf "mock build failed\\n"; exit 7) | "$1" -B "$2" capture --output "$3"'
        result = subprocess.run(['bash', '-c', script, 'offline-test', sys.executable,
                                 str(ROOT / 'scripts/native_cmux.py'), str(self.out)], capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertIn(b'mock build failed', (self.out / 'build-stdout.log').read_bytes())
        self.assertNotIn(b'mock build failed', result.stdout)

    def test_f5_stdout_redacts_tokens_without_authorization_header(self):
        secret = b'github_pat_' + b'X' * 40
        native.capture_build_stdout(io.BytesIO(secret), self.out)
        self.assertNotIn(secret, (self.out / 'build-stdout.log').read_bytes())


class D015Tests(unittest.TestCase):
    def workflow(self):
        return json.loads(subprocess.check_output([
            'ruby', '-ryaml', '-rjson', '-e',
            'puts JSON.generate(YAML.load_file(ARGV[0]))', str(ROOT / native.WORKFLOW)], text=True))

    def test_f1_current_production_recipe_publishes_not_deletes_complete_bundle(self):
        # Read the ACTUAL production YAML, not a proposed command or a source rewrite.
        steps = self.workflow()['jobs']['native-build']['steps']
        build = next(s['run'] for s in steps if s.get('id') == 'build')
        command = next(l for l in build.splitlines() if './scripts/reload.sh' in l)
        self.assertNotIn('--build-only', command,
                         'Pinned EXIT finalizer deletes the assembled staging bundle in this mode')
        self.assertNotIn('--launch', command)
        self.assertIn('--no-global-cli-links', command)
        self.assertIn('CMUX_DEV_BACKEND_MODE=local', command)
        self.assertEqual(build.count('./scripts/reload.sh'), 1)

    def test_f1_tags_must_already_equal_stock_slug(self):
        for bad in (TAG + '-', TAG.replace('exclusive', 'exclusive--slot'), TAG.upper(), 'd014-' + 'a' * 60):
            self.assertFalse(native.tag_ok(bad), bad)
        self.assertTrue(native.tag_ok(TAG))


class D015RecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.out = self.base / 'evidence'
        self.out.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        hashes = {}
        for relative in native.SOURCE_FILES:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('offline source fixture: ' + relative)
            hashes[relative] = native.sha256(path)
        from provider_fixture import pin_source
        pin_source(self, self.source)
        self.addCleanup(patch.stopall)
        patch.object(native, 'SOURCE_FILES', hashes).start()
        patch.object(native.sys, 'platform', 'darwin').start()
        patch.object(native.Path, 'home', return_value=self.home).start()
        # Never inspect/mutate this developer machine's native state in mock tests.
        patch.object(native.Path, 'glob', return_value=[]).start()
        patch.object(native.tempfile, 'gettempdir', return_value=str(self.base)).start()
        self.env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                    'GITHUB_REPOSITORY': native.CI_REPO, 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_SHA': WORKFLOW_SHA, 'EXPECTED_WORKFLOW_SHA': WORKFLOW_SHA,
                    'GITHUB_RUN_ID': str(RUN_ID), 'RUNNER_TEMP': str(self.base),
                    'GITHUB_ENV': str(self.base / 'env'), 'GITHUB_PATH': str(self.base / 'path')}
        patch.dict(native.os.environ, self.env, clear=True).start()
        self.calls = []
        patch.object(native, 'run', side_effect=self.fake_run).start()

    def fake_run(self, *args, cwd=None, env=None, timeout=180):
        self.calls.append((args, env, timeout))
        if args == ('git', 'rev-parse', 'HEAD'):
            return native.SOURCE_SHA if cwd == self.source else WORKFLOW_SHA
        return {('git', 'rev-parse', 'HEAD^{tree}'): native.SOURCE_TREE,
                ('git', 'submodule', 'status', '--recursive'): ' ' + 'b' * 40 + ' ghostty',
                ('uname', '-m'): 'x86_64',
                ('xcodebuild', '-version'): 'Xcode 26.6\nBuild version 17F113',
                ('bash', 'scripts/ghostty-zig-version.sh', str(self.source)): '0.99.1',
                }.get(args, '')  # 0.99.1 is deliberately only a fixture, not a source claim.

    def prepare(self):
        report = {'ok': True}
        native.prepare_recipe(self.source, TAG, self.out, report)
        (self.out / 'prepare.json').write_text(json.dumps(report))
        return report

    def final_app(self):
        app = native.recipe_paths(TAG)[2]
        (app / 'Contents').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleName': 'cmux DEV ' + TAG,
            'CFBundleIdentifier': 'com.cmuxterm.app.debug.' + TAG.replace('-', '.'),
            'LSEnvironment': {'CMUX_TAG': TAG, 'CMUX_PORT': '12345',
                              'CMUX_BUNDLED_CLI_PATH': str(app / 'Contents/Resources/bin/cmux'),
                              'CMUX_SOCKET_PATH': '/tmp/cmux-debug-' + TAG + '.sock',
                              'CMUX_API_BASE_URL': 'http://localhost:12345',
                              'CMUX_VM_API_BASE_URL': 'http://localhost:12345'}}))
        return app

    def test_prepare_retains_exact_source_and_final_path(self):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            report = self.prepare()
        before, after = [json.loads(line.removeprefix('prepare FD: ')) for line in log.getvalue().splitlines()]
        evidence = report['prepare_source_fds']
        self.assertEqual(before['checkpoint'], 'before')
        self.assertEqual(after, dict(evidence, checkpoint='after_cleanup'))
        self.assertEqual(evidence['phase'], 'prepare.source_contract')
        self.assertEqual(evidence['result'], 'passed')
        self.assertEqual(evidence['platform'], sys.platform)
        self.assertEqual(evidence['python'], sys.version.split()[0])
        self.assertEqual(evidence['machine'], os.uname().machine)
        self.assertEqual(evidence['open_fds_before'], evidence['open_fds_after'])
        self.assertEqual(evidence['open_fd_delta'], 0)
        import resource
        soft, hard = ('unlimited' if n == resource.RLIM_INFINITY else n
                      for n in resource.getrlimit(resource.RLIMIT_NOFILE))
        self.assertEqual(before['rlimit_nofile'], {'soft': soft, 'hard': hard})
        if sys.platform == 'darwin':
            ceiling = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.maxfilesperproc']))
            self.assertEqual(before['darwin_maxfilesperproc'], ceiling)
        self.assertEqual(report['source_tree'], native.SOURCE_TREE)
        self.assertEqual(report['tag_slug'], TAG)
        self.assertEqual(report['backend'], 'local')
        self.assertTrue(report['app'].endswith('/Build/Products/Debug/cmux DEV ' + TAG + '.app'))
        for relative, expected in native.SOURCE_FILES.items():
            self.assertEqual(native.sha256(self.out / ('source-proof--' + relative.replace('/', '--'))), expected)
        self.assertFalse(Path(report['derived_data']).exists())
        self.assertIn('NATIVE_APP_PATH=' + report['app'], Path(self.env['GITHUB_ENV']).read_text())

    def test_prepare_source_rejection_keeps_fd_evidence_and_gate(self):
        report = {}
        error = ValueError('fixture source proof rejected')
        log = io.StringIO()

        def reject(*args):
            before, = log.getvalue().splitlines()
            self.assertEqual(json.loads(before.removeprefix('prepare FD: '))['checkpoint'], 'before')
            with tempfile.TemporaryFile():
                raise error

        with contextlib.redirect_stdout(log), patch.object(native, 'source_contract', side_effect=reject):
            with self.assertRaises(ValueError) as raised:
                native.prepare_recipe(self.source, TAG, self.out, report)
        self.assertIs(raised.exception, error)
        self.assertEqual(report['prepare_source_fds']['result'], 'failed')
        self.assertEqual(report['prepare_source_fds']['error_type'], 'ValueError')
        self.assertEqual(report['prepare_source_fds']['open_fd_delta'], 0)
        self.assertIn('after_cleanup', log.getvalue())
        self.assertNotIn(str(error), log.getvalue())
        self.assertFalse(native.recipe_paths(TAG)[0].exists())
        self.assertFalse(Path(self.env['GITHUB_ENV']).exists())

    def test_prepare_fd_snapshot_excludes_observer_and_is_bounded(self):
        before = native._prepare_open_fd_count()
        self.assertIsInstance(before, int)
        with tempfile.TemporaryFile():
            self.assertEqual(native._prepare_open_fd_count(), before + 1)
        self.assertEqual(native._prepare_open_fd_count(), before)
        with patch.object(native, '_PREPARE_FD_SCAN_CAP', 0):
            self.assertIsNone(native._prepare_open_fd_count())
        with patch.object(native.os, 'scandir', side_effect=PermissionError):
            self.assertIsNone(native._prepare_open_fd_count())

    def diagnostics_cli(self):
        collect = native.collect_build_logs
        with patch.object(native, 'collect_build_logs', side_effect=lambda output, tag:
                          collect(output, tag, temp_root=self.base / 'native-log-input')), \
             patch.object(sys, 'argv', ['native_cmux.py', 'diagnostics', '--tag', TAG, '--output', str(self.out)]), \
             contextlib.redirect_stdout(io.StringIO()):
            code = native.main()
        return code, json.loads((self.out / 'diagnostics.json').read_text())

    def test_source_proofs_preserve_flat_final_upload_and_size_cap_contract(self):
        self.prepare()
        code, report = self.diagnostics_cli()
        self.assertEqual(code, 0)
        self.assertTrue(report['ok'])
        stage = self.out / 'upload'
        self.assertFalse((self.out / 'artifact-files.json').exists())
        manifest = json.loads((stage / 'artifact-files.json').read_text())
        rows = {row['path']: row for row in manifest['files']}
        proofs = {'source-proof--' + relative.replace('/', '--'): digest
                  for relative, digest in native.SOURCE_FILES.items()}
        self.assertEqual({name for name in rows if name.startswith('source-proof--')}, set(proofs))
        self.assertTrue(all(p.is_file() and p.suffix != '.zip' for p in stage.iterdir()))
        for name, digest in proofs.items():
            self.assertEqual(rows[name]['sha256'], digest)
            self.assertEqual(rows[name]['bytes'], (stage / name).stat().st_size)
            self.assertEqual(native.sha256(stage / name), digest)
            self.assertNotEqual((stage / name).stat().st_ino, (self.out / name).stat().st_ino)

        # Fresh production staging: only proofs consume this cap. Dropping them
        # would incorrectly pass, unlike the old cap10/reused-stage assertion.
        capped = self.base / 'capped-proof-evidence'
        capped.mkdir()
        for name in proofs:
            shutil.copyfile(self.out / name, capped / name)
        proof_bytes = sum((capped / name).stat().st_size for name in proofs)
        with patch.object(native, 'MAX_UPLOAD', 64 * 1024 + proof_bytes - 1):
            with self.assertRaisesRegex(ValueError, 'upload cap'):
                native.finalize_evidence(capped)
        self.assertEqual({p.name for p in (capped / 'upload').iterdir()}, set(proofs))

    def test_changed_source_proof_rejected_by_diagnostics_cli(self):
        self.prepare()
        relative = next(iter(native.SOURCE_FILES))
        (self.out / ('source-proof--' + relative.replace('/', '--'))).write_bytes(b'changed offline proof')
        code, report = self.diagnostics_cli()
        self.assertEqual(code, 1)
        self.assertFalse(report['ok'])
        self.assertIn('Retained pinned source proof mismatch', report['error'])

    def test_unlisted_source_proof_is_not_uploaded_by_diagnostics_cli(self):
        self.prepare()
        unknown = 'source-proof--unlisted-fixture.sh'
        (self.out / unknown).write_bytes(b'unlisted offline diagnostic fixture')
        code, report = self.diagnostics_cli()
        self.assertEqual(code, 0)
        self.assertTrue(report['ok'])
        self.assertFalse((self.out / 'upload' / unknown).exists())

    def test_tools_recheck_ci_identity_even_if_preparation_file_exists(self):
        self.prepare()
        with patch.dict(native.os.environ, {'RUNNER_ENVIRONMENT': 'self-hosted'}):
            with self.assertRaisesRegex(ValueError, 'CI runner only'):
                native.setup_tools(self.source, TAG, self.out, {})
        self.assertFalse((native.recipe_paths(TAG)[0] / 'tools').exists())

    def test_source_moving_head_tree_or_changed_helper_fails(self):
        for pin in ('SOURCE_SHA', 'SOURCE_TREE'):
            with patch.object(native, pin, 'f' * 40):
                with self.assertRaisesRegex(ValueError, 'Pinned source:'):
                    native.source_contract(self.source, self.out, {})
        (self.source / 'scripts/reload.sh').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'tracked bytes/target changed'):
            native.source_contract(self.source, self.out, {})

    def test_fresh_runner_rerun_injection_and_xcode_guards(self):
        for key, value in (('RUNNER_ENVIRONMENT', 'self-hosted'), ('GITHUB_RUN_ATTEMPT', '2'),
                           ('GITHUB_ACTIONS', 'false'), ('GITHUB_REPOSITORY', 'other/repo'),
                           ('GITHUB_SHA', native.SOURCE_SHA), ('BASH_ENV', 'inject'),
                           ('CMUX_DERIVED_DATA', '/shared'), ('ZIG_REQUIRED', 'moving')):
            with patch.dict(native.os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    self.prepare()
        with patch.object(native, 'run', return_value='wrong selected Xcode'):
            with self.assertRaises(ValueError):
                self.prepare()

    def test_existing_paths_and_dangling_symlinks_are_not_reused(self):
        root = native.recipe_paths(TAG)[0]
        root.symlink_to(self.base / 'absent')
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.prepare()
        root.unlink()
        state = self.home / 'Library/Application Support/cmux'
        state.parent.mkdir(parents=True)
        state.symlink_to(self.base / 'absent')
        with self.assertRaisesRegex(ValueError, 'Existing cmux'):
            self.prepare()

    def test_existing_process_or_launchd_label_is_rejected(self):
        original = self.fake_run
        for command in (('ps', '-axo', 'comm='), ('/bin/launchctl', 'list')):
            with patch.object(native, 'run', side_effect=lambda *a, cwd=None: '/Applications/cmux.app/Contents/MacOS/cmux' if a == command else original(*a, cwd=cwd)):
                with self.assertRaisesRegex(ValueError, 'Existing cmux'):
                    self.prepare()

    def test_materialization_requires_actual_final_tag_not_base_or_staging(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'final tagged app'):
            native.materialized_recipe(self.source, TAG, self.out, {})
        app = self.final_app()
        report = {}
        native.materialized_recipe(self.source, TAG, self.out, report)
        self.assertEqual(report['app'], str(app))
        # Publication alone deliberately does not claim helper/inventory compatibility.
        self.assertIn('pending', report['complete_bundle_validation'])
        info = app / 'Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleName': 'cmux DEV'}))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            native.materialized_recipe(self.source, TAG, self.out, {})

    def test_published_app_with_remote_backend_or_wrong_slug_is_rejected(self):
        self.prepare()
        app = self.final_app()
        path = app / 'Contents/Info.plist'
        original = plistlib.loads(path.read_bytes())
        for key, value in (('CMUX_TAG', 'other-tag'), ('CMUX_API_BASE_URL', 'https://remote.invalid'),
                           ('CMUX_DEV_BACKEND_URL', 'https://remote.invalid'),
                           ('CMUX_BUNDLED_CLI_PATH', '/base.app/cmux')):
            info = deepcopy(original)
            info['LSEnvironment'][key] = value
            path.write_bytes(plistlib.dumps(info))
            with self.assertRaisesRegex(ValueError, 'tagged/local'):
                native.materialized_recipe(self.source, TAG, self.out, {})
        path.write_bytes(plistlib.dumps(original))
        with patch.object(native.Path, 'glob', return_value=[app.parent / '.leftover-staging.app']):
            with self.assertRaisesRegex(ValueError, 'staging bundle'):
                native.materialized_recipe(self.source, TAG, self.out, {})

    def test_redirected_final_app_or_changed_prepare_contract_is_rejected(self):
        self.prepare()
        app = self.final_app()
        moved = app.with_name('redirect.app')
        app.rename(moved)
        app.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            native.materialized_recipe(self.source, TAG, self.out, {})
        app.unlink()
        moved.rename(app)
        data = json.loads((self.out / 'prepare.json').read_text())
        data['app'] = str(moved)
        (self.out / 'prepare.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            native.materialized_recipe(self.source, TAG, self.out, {})

    def test_tools_force_source_installer_local_and_never_repeat(self):
        self.prepare()
        original = self.fake_run
        def setup(*args, cwd=None, env=None, timeout=180):
            if args == ('bash', 'scripts/install-zig-ci.sh'):
                self.assertEqual(env['ZIG_FORCE_LOCAL_INSTALL'], '1')
                self.assertEqual(env['ZIG_REQUIRED'], '0.99.1')
                self.assertEqual(env['ZIG_DOWNLOAD_BUDGET_SECONDS'], '360')
                self.assertNotIn('GITHUB_ENV', env)
                self.assertNotIn('GITHUB_PATH', env)
                zig = Path(env['ZIG_INSTALL_ROOT']) / 'zig-x86_64-macos-0.99.1/zig'
                zig.parent.mkdir(parents=True)
                zig.write_text('offline zig fixture')
            if args[-1:] == ('version',):
                return '0.99.1'
            return original(*args, cwd=cwd, env=env, timeout=timeout)
        with patch.object(native, 'run', side_effect=setup), patch.object(native, 'install_bun'):
            report = {}
            native.setup_tools(self.source, TAG, self.out, report)
            self.assertIn('no component download', report['metal_setup'])
            self.assertNotIn('CMUX_ZIG', Path(self.env['GITHUB_PATH']).read_text())
            with self.assertRaisesRegex(ValueError, 'cannot repeat'):
                native.setup_tools(self.source, TAG, self.out, {})
        self.assertFalse(any('reload.sh' in a for a, _, _ in self.calls))

    def test_cold_metal_setup_uses_one_component_download_and_no_build(self):
        self.prepare()
        original = self.fake_run
        calls = []
        metal_checks = 0
        def setup(*args, cwd=None, env=None, timeout=180):
            nonlocal metal_checks
            calls.append(args)
            if args == ('bash', 'scripts/install-zig-ci.sh'):
                zig = Path(env['ZIG_INSTALL_ROOT']) / 'zig-x86_64-macos-0.99.1/zig'
                zig.parent.mkdir(parents=True)
                zig.write_text('offline compiler fixture')
            if args[-1:] == ('version',):
                return '0.99.1'
            if args == ('xcrun', '--sdk', 'macosx', 'metal', '--version'):
                metal_checks += 1
                if metal_checks == 1:
                    raise ValueError('mock missing Metal component')
            return original(*args, cwd=cwd, env=env, timeout=timeout)
        with patch.object(native, 'run', side_effect=setup), patch.object(native, 'install_bun'):
            report = {}
            native.setup_tools(self.source, TAG, self.out, report)
        self.assertEqual(calls.count(('xcodebuild', '-downloadComponent', 'MetalToolchain')), 1)
        self.assertEqual(metal_checks, 2)
        self.assertIn('one official', report['metal_setup'])
        self.assertFalse(any('reload.sh' in a for a in calls))

    def test_installer_failure_stops_without_tool_or_build_retry(self):
        self.prepare()
        original = self.fake_run
        calls = []
        def fail(*args, **kwargs):
            calls.append(args)
            if args == ('bash', 'scripts/install-zig-ci.sh'):
                raise ValueError('mock installer transfer budget exhausted')
            return original(*args, **kwargs)
        with patch.object(native, 'run', side_effect=fail), patch.object(native, 'install_bun') as bun:
            with self.assertRaisesRegex(ValueError, 'budget exhausted'):
                native.setup_tools(self.source, TAG, self.out, {})
            with self.assertRaisesRegex(ValueError, 'cannot repeat'):
                native.setup_tools(self.source, TAG, self.out, {})
            bun.assert_not_called()
        self.assertEqual(calls.count(('bash', 'scripts/install-zig-ci.sh')), 1)
        self.assertFalse(any('reload.sh' in a for a in calls))

    def test_metal_uses_only_one_official_component_call_when_needed(self):
        # Static order/call contract complements mocks; no Apple tool is executed.
        import inspect
        code = inspect.getsource(native.setup_tools)
        self.assertEqual(code.count('"-downloadComponent", "MetalToolchain"'), 1)
        self.assertIn('except ValueError:', code)
        self.assertNotIn('sudo', code.replace('# Apple\'s selected-Xcode component mechanism; no sudo/login/OS grant.', ''))

    def test_official_versioned_bun_archive_checksum_and_size_fail_closed(self):
        tools = self.base / 'bun-tools'
        tools.mkdir()
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as archive:
            info = zipfile.ZipInfo('bun-darwin-x64/bun')
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, b'offline bun executable fixture')
        archive_bytes = blob.getvalue()
        urls = []
        def download(*args, **kwargs):
            if args[0] == 'curl':
                urls.append(args[-1])
                target = Path(args[args.index('--output') + 1])
                if target.name == 'SHASUMS256.txt':
                    import hashlib
                    target.write_text(hashlib.sha256(archive_bytes).hexdigest() + '  bun-darwin-x64.zip\n')
                else:
                    target.write_bytes(archive_bytes)
                return ''
            return native.BUN_VERSION
        with patch.object(native, 'run', side_effect=download):
            report = {}
            native.install_bun(tools, report)
        self.assertTrue(all('/bun-v1.3.6/' in url for url in urls))
        self.assertTrue((tools / 'bun/bun').is_file())
        self.assertIn('same-origin', report['bun_verification'])
        # Tampering must fail BEFORE extraction or a version probe.
        shutil.rmtree(tools / 'bun')
        bad = blob.getvalue() + b'tamper'
        def corrupt(*args, **kwargs):
            result = download(*args, **kwargs)
            if args[0] == 'curl' and args[-1].endswith('.zip'):
                Path(args[args.index('--output') + 1]).write_bytes(bad)
            return result
        with patch.object(native, 'run', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                native.install_bun(tools, {})
        self.assertFalse((tools / 'bun').exists())

    def test_workflow_prepares_tools_once_then_validates_capture_before_inspect(self):
        steps = D015Tests().workflow()['jobs']['native-build']['steps']
        ids = [s.get('id') for s in steps]
        self.assertLess(ids.index('prepare'), ids.index('tools'))
        self.assertLess(ids.index('tools'), ids.index('preflight'))
        self.assertLess(ids.index('build'), ids.index('materialized'))
        self.assertLess(ids.index('materialized'), ids.index('inspect'))
        for mode in ('inspect', 'package'):
            command = next(s['run'] for s in steps if s.get('id') == mode)
            self.assertIn('--app "$NATIVE_APP_PATH"', command)
        commands = '\n'.join(s.get('run', '') for s in steps)
        self.assertEqual(commands.count('./scripts/reload.sh'), 1)
        self.assertNotIn('--build-only', commands)
        self.assertNotIn('--launch', commands)


class D019PackageRepairTests(unittest.TestCase):
    """Real per-path synthetic helpers; no native tools, launches or network."""
    HELPERS = ('Contents/Resources/bin/cmux', 'Contents/Resources/bin/cmuxd')
    fake_ditto = ReworkTests.fake_ditto
    package = ReworkTests.package

    def setUp(self):
        self.fixture = BundleTests('test_all_required_binaries_recorded_without_execution')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        # Twelve distinct stock/conditional host images; no standalone helper aliases.
        for relative in self.HELPERS:
            (self.app / relative).unlink(missing_ok=True)
            self.fixture.binary(relative)
        self.out = self.app.parent / 'evidence'
        self.out.mkdir()
        self.freeze()

    def freeze(self):
        report = self.fixture.inspect()
        report['ok'] = True
        (self.out / 'inspect.json').write_text(json.dumps(report))
        return report

    def replace_main(self, hardlink):
        main = self.fixture.main
        before = main.stat()
        outside = self.app.parent / 'outside-image'
        outside.write_bytes(main.read_bytes())
        outside.chmod(stat.S_IMODE(before.st_mode))
        main.unlink()
        if hardlink:
            os.link(outside, main)
        else:
            outside.rename(main)
        self.assertNotEqual(main.stat().st_ino, before.st_ino)
        self.assertEqual(main.stat().st_nlink, 2 if hardlink else 1)
        return outside

    def test_equal_byte_external_hardlink_replacement_refused(self):
        outside = self.replace_main(hardlink=True)
        self.assertEqual(self.fixture.main.stat().st_ino, outside.stat().st_ino)
        self.assertEqual(self.package(), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_equal_byte_single_link_replacement_refused(self):
        self.replace_main(hardlink=False)
        self.assertEqual(self.package(), 1)
        self.assertFalse((self.out / 'cmux-build-only.zip').exists())

    def test_existing_external_hardlink_refused_during_inspection(self):
        self.replace_main(hardlink=True)
        with self.assertRaisesRegex(ValueError, 'hardlink|link count'):
            self.fixture.inspect()

    def test_copy_binds_opened_file_identity_even_when_path_restored(self):
        main = self.fixture.main
        parent_inode = main.parent.stat().st_ino
        original_open = os.open
        for hardlink in (True, False):
            with self.subTest(hardlink=hardlink):
                expected = native.bundle_snapshot(self.app)
                outside = self.app.parent / 'copy-outside'
                outside.write_bytes(main.read_bytes())
                outside.chmod(0o755)
                saved = self.app.parent / 'saved-image'
                intercepted = []

                def swap(name, flags, *args, **kwargs):
                    if (name != main.name or 'dir_fd' not in kwargs or
                            os.fstat(kwargs['dir_fd']).st_ino != parent_inode):
                        return original_open(name, flags, *args, **kwargs)
                    intercepted.append(name)
                    main.rename(saved)
                    if hardlink:
                        os.link(outside, main)
                    else:
                        outside.rename(main)
                    try:
                        return original_open(name, flags, *args, **kwargs)
                    finally:
                        main.unlink()
                        saved.rename(main)

                with patch.object(native.os, 'open', side_effect=swap):
                    with self.assertRaisesRegex(ValueError, 'identity|hardlink|link count'):
                        native.copy_inspected_bundle(self.app, self.app.parent / f'copy-{hardlink}.app', expected)
                self.assertTrue(intercepted)
                restored = native.bundle_snapshot(self.app)
                self.assertEqual(restored['inventory'], expected['inventory'])
                relative = str(main.relative_to(self.app))
                # Bytes, mode and original inode really were restored. D019's
                # strengthened change identity additionally detects the swap.
                self.assertEqual(restored['identity']['files'][relative]['inode'],
                                 expected['identity']['files'][relative]['inode'])
                self.assertNotEqual(restored['identity']['files'][relative]['ctime_ns'],
                                    expected['identity']['files'][relative]['ctime_ns'])

    def test_missing_cmux_helper_refused(self):
        (self.app / self.HELPERS[0]).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing required source-defined helper: Contents/Resources/bin/cmux$'):
            self.fixture.inspect()

    def test_missing_cmuxd_helper_refused(self):
        # Historical identity retained; its first-pass unconditional premise was
        # wrong. reload.sh:2068-2096 is conditional; SOURCE_SHA has no cmuxd tree.
        (self.app / self.HELPERS[1]).unlink()
        report = self.fixture.inspect()
        self.assertNotIn(self.HELPERS[1], {item['path'] for item in report['binaries']})
        self.assertIn('absent', report['conditional_products']['cmuxd'])

    def test_both_missing_standalone_helpers_refused(self):
        for relative in self.HELPERS:
            (self.app / relative).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing required source-defined helper'):
            self.fixture.inspect()

    def test_required_standalone_helpers_must_be_macho(self):
        for relative in self.HELPERS:
            with self.subTest(helper=relative):
                path = self.app / relative
                path.write_bytes(b'not a Mach-O executable')
                with self.assertRaisesRegex(ValueError, 'Missing required source-defined helper'):
                    self.fixture.inspect()
                self.fixture.binary(relative)

    def test_required_standalone_helpers_must_be_executable(self):
        for relative in self.HELPERS:
            with self.subTest(helper=relative):
                path = self.app / relative
                path.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'Required helper has no execute bit'):
                    self.fixture.inspect()
                path.chmod(0o755)

    def test_required_standalone_helpers_must_be_mh_execute(self):
        for relative in self.HELPERS:
            with self.subTest(helper=relative):
                path = self.app / relative

                def execute(*args):
                    if args[:4] == ('otool', '-arch', 'x86_64', '-hv') and Path(args[-1]) == path:
                        return self.fixture.execute(*args).replace('EXECUTE', 'DYLIB')
                    return self.fixture.execute(*args)

                with self.assertRaisesRegex(ValueError, 'Required helper is not MH_EXECUTE'):
                    native.inspect_app(self.app, {}, execute)

    def test_required_standalone_helpers_use_existing_arch_and_minos_checks(self):
        for relative in self.HELPERS:
            path = self.app / relative
            for invalid in ('arm-only', 'newer-macos'):
                with self.subTest(helper=relative, invalid=invalid):
                    self.fixture.arch[path] = 'arm64' if invalid == 'arm-only' else 'x86_64'
                    self.fixture.loads[path] = commands('16.0' if invalid == 'newer-macos' else '14.0')
                    with self.assertRaises(ValueError):
                        self.fixture.inspect()
            self.fixture.arch.clear()
            self.fixture.loads.clear()

    def test_complete_distinct_helper_fixture_inspects_and_packages(self):
        report = self.freeze()
        self.assertEqual(len(report['binaries']), 12)
        for relative in self.HELPERS:
            self.assertIn(relative, {item['path'] for item in report['binaries']})
            self.assertFalse((self.app / relative).is_symlink())
        self.assertEqual(self.package(), 0)
        with zipfile.ZipFile(self.out / 'cmux-build-only.zip') as archive:
            for relative in self.HELPERS:
                self.assertIn(self.app.name + '/' + relative, archive.namelist())


if __name__ == '__main__':
    unittest.main()
