"""Real Git in owned temporary fixtures; no builds, network or candidate Git writes."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_cmux as native
import native_cmux_providers as providers
import native_cmux_fence as fences
import native_cmux_source as source_reader


def _open_fd_snapshot():
    """Observe live descriptors without holding a /dev/fd enumeration fd."""
    import errno
    import fcntl

    result = set()
    for entry in os.listdir("/dev/fd"):
        if not entry.isdecimal():
            continue
        fd = int(entry)
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            result.add(fd)
    return result


def _check_wide_source_contract(limit, source, output, commit, tree):
    """Child only: lower its own fd limit and check repeated source proofs."""
    import errno
    import fcntl
    import resource

    limit, source, output = int(limit), Path(source), Path(output)
    native.SOURCE_SHA, native.SOURCE_TREE = commit, tree
    native.SOURCE_FILES = {"tracked": native.sha256(source / "tracked")}
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))

    def open_fds():
        used = set()
        for fd in range(limit):
            try:
                fcntl.fcntl(fd, fcntl.F_GETFD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                used.add(fd)
        return used

    before = open_fds()
    previous = None
    for _ in range(3):
        report = {}
        native.source_contract(source, output, report)
        proof = report["source_inventory"]
        assert proof["tracked_files"] == 55 and proof["gitlinks"] == 1, proof
        assert previous is None or proof == previous, proof
        assert open_fds() == before, "source-contract descriptor leak"
        previous = proof
    assert (output / "source-proof--tracked").read_bytes() == (source / "tracked").read_bytes()


class RealSourceBytesTests(unittest.TestCase):
    def git(self, *args, cwd=None, data=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.check_output(["git", *args], cwd=cwd or self.source,
                                       input=data, stderr=subprocess.PIPE, env=env).decode().strip()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="source-byte-fixture-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        (self.source / "tracked").write_bytes(b"pinned source\n")
        (self.source / "nested").mkdir()
        (self.source / "nested/other").write_bytes(b"outside SOURCE_FILES\n")
        (self.source / "run").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.source / "run").chmod(0o755)
        (self.source / "link").symlink_to("tracked")
        lock = self.source / providers.LOCK
        lock.parent.mkdir(parents=True)
        lock.write_text('{"pins": []}\n')
        self.git("add", ".")
        self.git("commit", "-qm", "owned fixture")
        seed = self.root / "seed"
        seed.mkdir()
        self.git("init", "-q", cwd=seed)
        (seed / "seed-file").write_bytes(b"seed\n")
        self.git("add", ".", cwd=seed)
        self.git("commit", "-qm", "seed", cwd=seed)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(seed), "seed")
        self.git("commit", "-qam", "owned fixture with gitlink")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(native, "SOURCE_SHA", native.SOURCE_SHA))
        self.stack.enter_context(patch.object(native, "SOURCE_TREE", native.SOURCE_TREE))
        self.stack.enter_context(patch.object(native, "SOURCE_FILES", {}))
        self.stack.enter_context(patch.object(providers, "EXTRA_SOURCE", {
            providers.LOCK: native.sha256(self.source / providers.LOCK)}))
        self.pin()

    def pin(self):
        self.head = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        native.SOURCE_SHA = self.head
        native.SOURCE_TREE = self.tree

    def contract(self):
        report = {}
        native.source_contract(self.source, self.root / "output", report)
        return report

    def provider_contract(self):
        with providers._InputBindings() as bindings:
            return providers.source_proof(self.source, bindings)

    def assert_rejected(self):
        # Both independently reachable controlling paths must reject real bytes.
        for check in (self.contract, self.provider_contract):
            with self.subTest(path=check.__name__):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    check()

    def test_clean_real_git_source_and_report_exclusions(self):
        report = self.contract()
        self.provider_contract()
        self.assertEqual(report["source_head"], self.head)
        self.assertIn("source_inventory", report)
        self.assertEqual(report["source_inventory"]["tracked_files"], 7)
        self.assertIn("untracked", json.dumps(report["source_inventory"]["exclusions"]))

    def test_assume_unchanged_cannot_hide_bytes_outside_allowlist(self):
        self.git("update-index", "--assume-unchanged", "nested/other")
        (self.source / "nested/other").write_bytes(b"changed but index claims clean\n")
        self.assertEqual(self.git("diff", "HEAD", "--"), "")
        self.assert_rejected()

    def test_replacement_metadata_cannot_hide_changed_bytes(self):
        self.git("update-index", "--assume-unchanged", "tracked")
        replacement = self.root / "replacement.git"
        shutil.copytree(self.source / ".git", replacement)
        shutil.rmtree(self.source / ".git")
        shutil.copytree(replacement, self.source / ".git")
        (self.source / "tracked").write_bytes(b"metadata replacement is not source proof\n")
        self.assertEqual(self.git("diff", "HEAD", "--"), "")
        self.assert_rejected()

    def test_tracked_type_mode_symlink_missing_and_parent_failures(self):
        actions = ("type", "mode", "symlink", "missing", "parent")
        for action in actions:
            with self.subTest(failure=action):
                if action == "type":
                    (self.source / "tracked").unlink()
                    (self.source / "tracked").symlink_to("nested/other")
                elif action == "mode":
                    (self.source / "run").chmod(0o644)
                elif action == "symlink":
                    (self.source / "link").unlink()
                    (self.source / "link").symlink_to("nested/other")
                elif action == "missing":
                    (self.source / "tracked").unlink()
                else:
                    (self.source / "nested").rename(self.root / "moved")
                    (self.source / "nested").symlink_to(self.root / "moved", target_is_directory=True)
                self.assert_rejected()
                if action == "parent":
                    (self.source / "nested").unlink()
                    (self.root / "moved").rename(self.source / "nested")
                self.git("checkout", "-f", "HEAD", "--", ".")

    def test_forged_commit_object_under_pinned_name_rejected(self):
        # A real cat-file read, not mocked rev-parse: loose object payload does not
        # authenticate itself merely by occupying the pinned object's filename.
        import zlib
        obj = self.source / ".git/objects" / self.head[:2] / self.head[2:]
        raw = zlib.decompress(obj.read_bytes()).replace(b"owned fixture", b"forged fixture")
        obj.chmod(0o644)
        obj.write_bytes(zlib.compress(raw))
        self.assert_rejected()

    def test_forged_tree_and_blob_objects_rejected(self):
        import zlib
        blob = self.git("rev-parse", "HEAD:tracked")
        for oid in (self.tree, blob):
            with self.subTest(object=oid):
                path = self.source / ".git/objects" / oid[:2] / oid[2:]
                original = path.read_bytes()
                raw = bytearray(zlib.decompress(original))
                raw[-1] ^= 1  # Valid compressed object at the wrong canonical ID.
                path.chmod(0o644)
                path.write_bytes(zlib.compress(raw))
                self.assert_rejected()
                path.write_bytes(original)

    def test_missing_blob_object_rejected(self):
        oid = self.git("rev-parse", "HEAD:tracked")
        (self.source / ".git/objects" / oid[:2] / oid[2:]).unlink()
        self.assert_rejected()

    def test_index_only_disagreement_and_excluded_inputs_are_not_authority(self):
        (self.source / "untracked-generated").write_bytes(b"not claimed as verified\n")
        self.git("update-index", "--cacheinfo", "100755," + self.git("rev-parse", "HEAD:tracked") + ",tracked")
        # Index differs, but actual source still exactly matches the pinned tree.
        self.contract()
        self.provider_contract()

    def test_atime_only_read_effect_is_not_a_source_mutation(self):
        path = self.source / "tracked"
        os.utime(path, ns=(1, path.stat().st_mtime_ns))
        first = self.contract()["source_inventory"]
        self.assertEqual(self.contract()["source_inventory"], first)

    def test_skip_worktree_cannot_hide_bytes(self):
        self.git("update-index", "--skip-worktree", "tracked")
        (self.source / "tracked").write_bytes(b"skip-worktree is not source authority\n")
        self.assert_rejected()

    def test_preflight_rejects_changed_source_before_platform_tools(self):
        self.git("update-index", "--assume-unchanged", "nested/other")
        (self.source / "nested/other").write_bytes(b"bad preflight source\n")
        actual_platform = sys.platform
        # Rebind only native's platform view. The real fence module keeps the
        # actual kernel's sys.platform (inotify on Linux, kqueue on macOS).
        with patch.object(native, "sys", SimpleNamespace(platform="darwin")), \
             patch.object(native, "run", side_effect=AssertionError("must reject before native probes")) as probes:
            self.assertEqual(sys.platform, actual_platform)
            self.assertIs(fences.sys, sys)
            self.assertEqual(fences.sys.platform, actual_platform)
            with self.assertRaisesRegex(ValueError, "Pinned source:"):
                native.preflight(self.source, "d014-source-byte-fixture", {})
            probes.assert_not_called()
        self.assertIs(native.sys, sys)
        self.assertEqual(sys.platform, actual_platform)

    def test_replace_ref_does_not_replace_pinned_object_authority(self):
        original = self.head
        (self.source / "tracked").write_bytes(b"replacement commit bytes\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "replacement")
        other = self.git("rev-parse", "HEAD")
        self.git("replace", original, other)
        self.git("update-ref", "HEAD", original)
        self.assert_rejected()

    def add_submodules(self):
        child = self.root / "child"
        child.mkdir()
        self.git("init", "-q", cwd=child)
        (child / "child-file").write_bytes(b"pinned child\n")
        self.git("add", ".", cwd=child)
        self.git("commit", "-qm", "child", cwd=child)
        mid = self.root / "mid"
        mid.mkdir()
        self.git("init", "-q", cwd=mid)
        (mid / "mid-file").write_bytes(b"mid\n")
        self.git("add", ".", cwd=mid)
        self.git("commit", "-qm", "mid", cwd=mid)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "deep", cwd=mid)
        self.git("commit", "-qam", "nested child", cwd=mid)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(mid), "module")
        self.git("-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
        self.git("commit", "-qam", "module")
        self.pin()
        return self.source / "module/deep"

    def test_recursive_gitlinks_clean_then_assume_unchanged_child_bytes_fail(self):
        child = self.add_submodules()
        report = self.contract()
        self.provider_contract()
        self.assertEqual(report.get("source_inventory", {}).get("gitlinks"), 3)
        self.git("update-index", "--assume-unchanged", "child-file", cwd=child)
        (child / "child-file").write_bytes(b"changed recursive source\n")
        self.assert_rejected()

    def test_uninitialized_gitlink_fails(self):
        self.add_submodules()
        shutil.rmtree(self.source / "module")
        (self.source / "module").mkdir()
        self.assert_rejected()

    def test_wrong_gitlink_head_fails(self):
        child = self.add_submodules()
        (child / "new").write_text("new child revision")
        self.git("add", ".", cwd=child)
        self.git("commit", "-qm", "wrong child revision", cwd=child)
        self.assert_rejected()

    def test_valid_large_tree_uses_high_fd_poll_and_reaps_readers(self):
        import resource

        limits = resource.getrlimit(resource.RLIMIT_NOFILE)
        for i in range(1100):
            directory = self.source / "fanout" / f"leaf-{i:04}"
            directory.mkdir(parents=True)
            (directory / "proof").write_text(f"valid tracked leaf {i}\n")
        self.git("add", "fanout")
        self.git("commit", "-qm", "valid large source fixture")
        self.pin()
        output = self.root / "high-fd-evidence"
        output.mkdir()
        children, registrations, waits = [], [], []
        popen, poll_factory = subprocess.Popen, source_reader.select.poll

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        class ObservedPoll:
            def __init__(self):
                self.delegate = poll_factory()

            def register(self, descriptor, events):
                registrations.append(descriptor.fileno())
                return self.delegate.register(descriptor, events)

            def poll(self, timeout):
                waits.append(timeout)
                return self.delegate.poll(timeout)

        baseline = _open_fd_snapshot()
        proof = None
        with patch.object(subprocess, "Popen", side_effect=spawn), \
             patch.object(source_reader.select, "poll", side_effect=ObservedPoll), \
             patch.object(native, "SOURCE_FILES", {"tracked": native.sha256(self.source / "tracked")}):
            for _ in range(2):
                report = {}
                native.source_contract(self.source, output, report)
                self.assertEqual(_open_fd_snapshot(), baseline)
                inventory = report["source_inventory"]
                self.assertGreaterEqual(inventory["tracked_files"], 1100)
                self.assertEqual(inventory["gitlinks"], 1)
                self.assertTrue(proof is None or proof == inventory)
                proof = inventory
        self.assertGreaterEqual(max(registrations), 1024)
        self.assertTrue(waits and all(1 <= value <= 180000 for value in waits))
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), limits)
        self.assertTrue(children and all(child.returncode == 0 for child in children))
        self.assertEqual(native.sha256(output / "source-proof--tracked"), native.sha256(self.source / "tracked"))
        print(json.dumps({"valid_large_tree": True, "max_git_pipe_fd": max(registrations),
                          "poll_timeout_max_ms": max(waits), "same_fd_set_after": True,
                          "reaped_children": len(children), "rlimit_unchanged": True}))

    def test_empty_poll_preserves_git_read_timeout_and_cleanup(self):
        poll_factory = source_reader.select.poll

        class EmptyPoll:
            def __init__(self):
                self.delegate = poll_factory()

            def register(self, descriptor, events):
                return self.delegate.register(descriptor, events)

            def poll(self, timeout):
                self_ready = self.delegate.poll(timeout)
                if not self_ready:
                    raise AssertionError("valid fixture Git reader did not become ready")
                return []  # Ordinary readiness-timeout fault injection, no input mutation.

        baseline = _open_fd_snapshot()
        with patch.object(source_reader.select, "poll", side_effect=EmptyPoll):
            with self.assertRaises(ValueError) as raised:
                self.contract()
        errors = []
        error = raised.exception
        while error is not None:
            errors.append(str(error))
            error = error.__context__
        self.assertIn("Pinned source: Git object read timed out", errors)
        self.assertEqual(_open_fd_snapshot(), baseline)

    def test_wide_source_contract_under_reduced_fd_limit(self):
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            self.skipTest("no supported directory-fence backend")
        import resource

        limit = 128
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY and soft < limit:
            self.skipTest(f"ambient fd limit {soft} is already below {limit}")
        for i in range(48):
            directory = self.source / "fanout" / "shared" / f"leaf-{i:02}"
            directory.mkdir(parents=True)
            (directory / "proof").write_text(f"tracked path {i}\n")
        self.git("add", "fanout")
        self.git("commit", "-qm", "wide tracked fixture")
        self.pin()
        output = self.root / "output"
        output.mkdir()
        child = ("import sys; from test_native_cmux_source_bytes import _check_wide_source_contract; "
                 "_check_wide_source_contract(*sys.argv[1:])")
        result = subprocess.run([sys.executable, "-c", child, str(limit), str(self.source), str(output),
                                 self.head, self.tree], capture_output=True, text=True, timeout=90,
                                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent),
                                     "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provider_binding_lifetime_rechecks_nonallowlisted_source(self):
        with self.assertRaises(ValueError):
            with providers._InputBindings() as bindings:
                providers.source_proof(self.source, bindings)
                self.git("update-index", "--assume-unchanged", "nested/other")
                (self.source / "nested/other").write_bytes(b"mutated after source binding\n")


if __name__ == "__main__":
    unittest.main()
