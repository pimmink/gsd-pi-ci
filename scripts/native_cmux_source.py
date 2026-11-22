"""Pinned Git objects plus independent tracked-worktree proof; never trust the index.

This is not metadata identity resolution. Git locates objects; their canonical
hashes authenticate them. Every pinned tree entry is then checked with lstat and
no-follow reads, recursively including gitlink commits. Caller retains directory
watches across the entire candidate and compares inventories before publication.
"""
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import time


# The pinned Ghostty tree's .gitattributes requires CRLF for this one .cmd.
# Bind the checkout representation to the authenticated commit, path and blob;
# never consult local attributes, index flags, Git config or external filters.
_PINNED_CRLF_CHECKOUT = (
    "ef4f1570d62117ebc51e3b0f07ad4f34032267b6",
    "./ghostty/example/electron-embed-windows/scripts/start-software-renderer.cmd",
    "aaa36cb5a01e4f0612fc40ba150aca1d2e7d887d",
)


EXCLUSIONS = [
    "Git metadata and index contents/flags are not tracked-source byte authority",
    "untracked and ignored files, including generated/dependency/build inputs",
    "symlink referent contents except independently inventoried tracked entries",
    "Git-unrepresented ownership, ACLs, xattrs and non-executable permission bits",
]


def source_inventory(source, directory):
    with ExitStack() as processes:
        return _source_inventory(source, directory, processes)


def _source_inventory(source, directory, processes):
    import native_cmux as native
    require = native.require
    records, gitlinks = {}, []

    def git(repo, *args):
        try:
            # Ignore refs/replace; authenticate every returned object independently.
            return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repo), *args],
                                           stderr=subprocess.PIPE, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValueError("Pinned source: unavailable Git object/checkout evidence") from None

    readers = {}

    def object_data(repo, kind, oid):
        require(re.fullmatch(r"[0-9a-f]{40}", oid), "Pinned source: invalid object ID")
        if repo not in readers:
            # One read-only batch process per repo, not one process per blob.
            process = subprocess.Popen(["git", "--no-replace-objects", "-C", str(repo),
                                        "cat-file", "--batch"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            def close():
                process.stdin.close()
                process.stdout.close()
                try:
                    require(process.wait(timeout=5) == 0, "Pinned source: Git object reader failed")
                except subprocess.TimeoutExpired:
                    process.kill()  # Only this owned, read-only child; never external sessions.
                    process.wait()
                    raise ValueError("Pinned source: Git object reader did not exit") from None
            processes.callback(close)
            # poll supports pipe descriptors above select's FD_SETSIZE and does
            # not allocate another kernel descriptor per retained Git reader.
            poller = select.poll()
            poller.register(process.stdout, select.POLLIN)
            readers[repo] = process, bytearray(), poller
        process, buffer, poller = readers[repo]
        deadline = time.monotonic() + 180

        def receive(size=None):
            while (b"\n" not in buffer if size is None else len(buffer) < size):
                remaining = deadline - time.monotonic()
                require(remaining > 0 and poller.poll(math.ceil(remaining * 1000)),
                        "Pinned source: Git object read timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                require(chunk, "Pinned source: truncated/missing Git object")
                buffer.extend(chunk)
                require(size is not None or len(buffer) <= 65536,
                        "Pinned source: malformed Git object header")
            count = buffer.index(b"\n") + 1 if size is None else size
            result = bytes(buffer[:count])
            del buffer[:count]
            return result

        try:
            process.stdin.write(oid.encode() + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise ValueError("Pinned source: unavailable Git object reader") from None
        fields = receive().rstrip(b"\n").split(b" ")
        require(len(fields) == 3 and fields[0] == oid.encode() and fields[1] == kind.encode()
                and fields[2].isdigit(), "Pinned source: missing/wrong Git object type")
        size = int(fields[2])
        require(size <= 512 * 1024**2, "Pinned source: unsupported object size (>512 MiB)")
        raw = receive(size)
        require(receive(1) == b"\n", "Pinned source: malformed Git object framing")
        header = f"{kind} {len(raw)}\0".encode()
        require(hashlib.sha1(header + raw).hexdigest() == oid,
                "Pinned source: unauthenticated " + kind + " object")
        return raw

    def stamp(info):
        # Reading may legitimately advance atime; it is not content provenance.
        return {**native.file_identity(info), "mode": info.st_mode, "bytes": info.st_size}

    def identity(path):
        info = path.lstat()
        return info, stamp(info)

    def bind_directory(path, key):
        require(not path.is_symlink() and path.is_dir(), "Pinned source: wrong directory type: " + key)
        require(path.resolve(strict=True) == path,
                "Pinned source: nonphysical directory: " + key)
        directory(path)  # Persistent caller-owned watches, including each subtree.
        info = path.lstat()
        records[key] = {"type": "directory", "device": info.st_dev, "inode": info.st_ino}

    def tree(repo, path, oid, prefix, depth, commit_oid):
        require(depth < 128, "Pinned source: excessive tree/gitlink depth")
        bind_directory(path, prefix)
        raw = object_data(repo, "tree", oid)
        position, names = 0, set()
        while position < len(raw):
            end = raw.find(b"\0", position)
            require(end > position and end + 21 <= len(raw), "Pinned source: malformed tree")
            entry = raw[position:end].split(b" ", 1)
            require(len(entry) == 2, "Pinned source: malformed tree entry")
            mode, name = entry
            require(name not in names and name not in (b"", b".", b"..") and
                    name.lower() != b".git" and b"/" not in name,
                    "Pinned source: unsafe/duplicate tree path")
            names.add(name)
            child_oid = raw[end + 1:end + 21].hex()
            position = end + 21
            child, key = path / os.fsdecode(name), prefix + "/" + os.fsdecode(name)
            if mode == b"40000":
                tree(repo, child, child_oid, key, depth + 1, commit_oid)
            elif mode == b"160000":
                gitlinks.append({"path": key, "commit": child_oid})
                checkout(child, child_oid, None, key, depth + 1)
            else:
                require(mode in (b"100644", b"100755", b"120000"),
                        "Pinned source: unsupported tree mode")
                expected = object_data(repo, "blob", child_oid)
                if mode == b"100644" and (commit_oid, key, child_oid) == _PINNED_CRLF_CHECKOUT:
                    # Only the pinned blob (LF-only) is transformed. The file on disk
                    # must equal the exact CRLF bytes; its bytes are never normalized.
                    expected = expected.replace(b"\n", b"\r\n")
                before, proof = identity(child)
                if mode == b"120000":
                    require(stat.S_ISLNK(before.st_mode), "Pinned source: wrong symlink type: " + key)
                    actual = os.fsencode(os.readlink(child))
                else:
                    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                            "Pinned source: wrong/aliased regular file type: " + key)
                    require(before.st_size == len(expected), "Pinned source: tracked bytes/target changed: " + key)
                    require(before.st_mode & 0o111 == (0o111 if mode == b"100755" else 0),
                            "Pinned source: executable mode mismatch: " + key)
                    fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        require(stamp(os.fstat(stream.fileno())) == proof, "Pinned source: open race: " + key)
                        actual = stream.read()
                        require(stamp(os.fstat(stream.fileno())) == proof, "Pinned source: read race: " + key)
                require(stamp(child.lstat()) == proof and actual == expected,
                        "Pinned source: tracked bytes/target changed: " + key)
                records[key] = {"type": "symlink" if mode == b"120000" else "file",
                                "blob": child_oid, "sha256": hashlib.sha256(actual).hexdigest(),
                                "identity": proof}

    def checkout(repo, commit_oid, expected_tree, prefix, depth):
        bind_directory(repo, prefix)
        # Do not let an uninitialized gitlink accidentally use its parent's repo.
        top = git(repo, "rev-parse", "--show-toplevel").rstrip(b"\n")
        require(Path(os.fsdecode(top)).resolve(strict=True) == repo,
                "Pinned source: missing recursive checkout: " + prefix)
        require(git(repo, "rev-parse", "--verify", "HEAD").strip().decode() == commit_oid,
                "Pinned source: wrong checkout commit: " + prefix)
        commit = object_data(repo, "commit", commit_oid)
        first = commit.split(b"\n", 1)[0]
        require(re.fullmatch(b"tree [0-9a-f]{40}", first), "Pinned source: malformed commit tree")
        tree_oid = first[5:].decode()
        require(expected_tree is None or tree_oid == expected_tree, "Pinned source: wrong pinned tree")
        tree(repo, repo, tree_oid, prefix, depth, commit_oid)

    source = Path(source).absolute()
    checkout(source, native.SOURCE_SHA, native.SOURCE_TREE, ".", 0)
    encoded = json.dumps(records, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return {"commit": native.SOURCE_SHA, "tree": native.SOURCE_TREE,
            "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
            "tracked_files": sum(r["type"] != "directory" for r in records.values()),
            "gitlinks": len(gitlinks), "submodules": gitlinks, "exclusions": EXCLUSIONS}
