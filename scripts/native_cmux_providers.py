#!/usr/bin/env python3
"""Locked SwiftPM binary-distribution collector/verifier, after the STOCK build.

No network, package resolution, build, launch, or download. References are NOT
attestations: inspect/package rederive the contract from pinned source, provider
Git objects, physical manifests and checksum-verified cached archives every time.
Unsupported manifests/products and unavailable cache evidence fail closed.
"""
import argparse
from contextlib import ExitStack
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import struct
import subprocess
import sys
import zipfile

from native_cmux_fence import directory_fence

LOCK = "cmux.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
# Source-proven macOS products, not fabricated runtime helper names. The local
# product edges below and project/Debug recipe are hashed at the unchanged pin.
PRODUCTS = {"sparkle": "Sparkle", "sentry-cocoa": "Sentry", "iroh-ffi": "IrohLib"}
EXTRA_SOURCE = {
    "Packages/Shared/CmuxIrohTransport/Package.swift": "69295a0fef01254908d05a803d2430e2bf62d4698bc56148d60be681bab207c8",
    "Packages/Shared/CmuxSentryTelemetry/Package.swift": "600a8d2d8f55d27d2a65063808e2d1972965f3b8561bfdba1f71b5ba4b31ca29",
}
MAX_BYTES = 400 * 1024**2
# Exact Package.swift at the stock lock's ee19f156667ca640b912108a45f8b5bb8d156fec.
# Only this known conditional is selected here; arbitrary Swift remains unsupported.
IROH_MANIFEST_SHA256 = "176b8a1e0b51eb76392055f94c3c7dc77fc1805d9e8be689299e8801810409a9"
IROH_LOCAL_BINARIES = (
    "Iroh.xcframework/macos-arm64_x86_64/Iroh.framework/Versions/A/Iroh",
    "Iroh.xcframework/macos-x86_64_arm64/Iroh.framework/Versions/A/Iroh",
    "Iroh.xcframework/macos-arm64/Iroh.framework/Versions/A/Iroh",
)
DEVELOPMENT = {"Headers", "PrivateHeaders", "Modules"}


def development_member(relative):
    parts = PurePosixPath(relative).parts
    return bool(parts and (parts[0] in DEVELOPMENT or
                (len(parts) >= 3 and parts[0] == "Versions" and parts[2] in DEVELOPMENT)))


def signature_member(relative):
    parts = PurePosixPath(relative).parts
    return len(parts) >= 2 and parts[-2:] == ("_CodeSignature", "CodeResources")


def require(condition, message):
    if not condition:
        raise ValueError("Provider contract: " + message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bound(path):
    """Hash the actual no-follow, non-hardlinked input; persist physical identity."""
    import native_cmux as native
    path = Path(path).absolute()
    require(not path.is_symlink(), "symlink input")
    path = path.resolve(strict=True)
    with directory_fence(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_BYTES,
                    "input must be a bounded, single-link regular file")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            require(len(data) == before.st_size and native.file_identity(before) == native.file_identity(after),
                    "input changed during read")
        named = path.stat(follow_symlinks=False)
        require(native.file_identity(named) == native.file_identity(before), "input pathname changed")
    return data, {"path": str(path.resolve()), "sha256": digest(data), "bytes": len(data),
                  "identity": native.file_identity(before)}


def git_output(checkout, *args):
    with directory_fence(checkout):
        try:
            data = subprocess.check_output(["git", "-C", str(checkout), *args],
                                           stderr=subprocess.PIPE, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValueError("Provider contract: cannot read locked Git evidence; no fallback") from None
        require(len(data) <= 4 * 1024**2, "oversize Git evidence")
        return data


def git_object(checkout, kind, oid):
    require(re.fullmatch(r"[0-9a-f]{40}", oid), "invalid locked Git object identity")
    raw = git_output(checkout, "cat-file", kind, oid)
    actual = hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest()
    require(actual == oid, "locked Git object content hash mismatch")
    return raw


def git_blob(checkout, revision, path):
    # Also protect standalone callers across HEAD -> commit -> tree -> blob;
    # git_output's individual invocation fences cannot observe between-read ABA.
    with directory_fence(checkout):
        require(git_output(checkout, "show", "-s", "--format=%H", "HEAD").decode().strip() == revision,
                "provider/source checkout is not the locked revision")
        # Do not trust a loose object's filename or a working Package.swift. Verify
        # the complete commit -> tree -> blob cryptographic chain at the lock pin.
        commit = git_object(checkout, "commit", revision)
        first = commit.split(b"\n", 1)[0]
        require(first.startswith(b"tree "), "locked commit lacks root tree")
        oid = first[5:].decode()
        parts = PurePosixPath(path).parts
        for index, part in enumerate(parts):
            tree = git_object(checkout, "tree", oid)
            entries, position = {}, 0
            while position < len(tree):
                end = tree.index(b"\0", position)
                mode, name = tree[position:end].split(b" ", 1)
                require(name not in entries and end + 21 <= len(tree), "invalid locked Git tree")
                entries[name] = (mode, tree[end + 1:end + 21].hex())
                position = end + 21
            require(part.encode() in entries, "locked manifest missing from provider Git tree")
            mode, oid = entries[part.encode()]
            require(mode in ((b"100644", b"100755") if index == len(parts) - 1 else (b"40000",)),
                    "locked manifest is not a regular Git blob")
        return git_object(checkout, "blob", oid)


def source_hashes():
    import native_cmux as native
    return {**native.SOURCE_FILES, **EXTRA_SOURCE}


class _InputBindings(ExitStack):
    """One owner for input/ancestor watches, retained until the proof is sealed.

    Deduplicate exact input directories, not overlapping ancestor chains: a
    parent watch does not observe a descendant's own rename. Never close/rearm
    an earlier binding between discovery, reads or derivations.
    """
    def __init__(self):
        super().__init__()
        self.directories = set()
        self.sources = {}

    def source(self, path):
        from native_cmux_source import source_inventory
        path = Path(path).absolute()
        proof = source_inventory(path, self.directory)
        require(path not in self.sources or self.sources[path] == proof,
                "tracked source physical inventory changed during candidate")
        self.sources[path] = proof
        return proof

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                for path in list(self.sources):
                    self.source(path)
        except BaseException:
            super().__exit__(*sys.exc_info())
            raise
        return super().__exit__(kind, value, traceback)

    def directory(self, path):
        path = Path(path).absolute()
        if path not in self.directories:
            self.enter_context(directory_fence(path))
            self.directories.add(path)
        return path.resolve(strict=True)

    def read(self, path):
        self.directory(Path(path).absolute().parent)
        return read_bound(path)


def source_proof(source, bindings):
    import native_cmux as native
    result, lock = {"tracked_source_inventory": bindings.source(source)}, None
    for relative, expected in source_hashes().items():
        data, proof = bindings.read(source / relative)
        require(digest(data) == expected, "wrong stock producer/configuration/lock: " + relative)
        result[relative] = proof
        if relative == LOCK:
            lock = json.loads(data)
    require(lock is not None, "missing Package.resolved")
    # All tracked files and recursive gitlinks, not just this configuration list,
    # are authenticated above and rechecked while the same watches remain held.
    return result, {p["identity"]: p for p in lock["pins"]}


def split_args(text):
    result, start, depth, quoted, escaped = [], 0, 0, False, False
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(text[start:i].strip())
            start = i + 1
    result.append(text[start:].strip())
    return result


def calls(text, function):
    """Conservative literal Swift manifest subset; NEVER execute a manifest."""
    result = []
    for match in re.finditer(r"\." + function + r"\s*\(", text):
        depth, quoted, escaped, end = 1, False, False, match.end()
        for end in range(match.end(), len(text)):
            char = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
        require(depth == 0, "unterminated Swift manifest call")
        fields = {}
        for field in split_args(text[match.end():end]):
            if field:
                require(":" in field, "unsupported positional manifest expression")
                key, value = field.split(":", 1)
                fields[key.strip()] = value.strip()
        result.append(fields)
    return result


def iroh_probe_stat(path):
    """Only ENOENT proves absence; inaccessible/failed metadata is not evidence."""
    try:
        return path.lstat()
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise ValueError("Provider contract: Iroh local probe failed; absence is unproven") from exc


def selected_manifest(manifest, product, checkout, bindings):
    """Select only the exact pinned Iroh ternary, using its actual input conditions."""
    marker = "let irohBinary: Target = useLocalXcframework"
    if marker not in manifest:
        return manifest
    require(product == "IrohLib" and digest(manifest.encode()) == IROH_MANIFEST_SHA256,
            "unsupported/unpinned conditional provider manifest")
    require(checkout is not None and bindings is not None,
            "unproven Iroh remote/local selection inputs")
    checkout = bindings.directory(checkout)
    # Swift uses != nil, so even an empty value forces the release distribution.
    if "IROH_FORCE_REMOTE_XCFRAMEWORK" not in os.environ:
        for relative in IROH_LOCAL_BINARIES:
            path, parts = checkout, PurePosixPath(relative).parts
            for index, part in enumerate(parts):
                path = path / part
                observed = iroh_probe_stat(path)
                if observed is None:
                    break
                require(index < len(parts) - 1,
                        "selected local Iroh input is not a proven remote distribution")
                require(stat.S_ISDIR(observed.st_mode), "nonphysical Iroh selection input")
                bindings.directory(path)
    # The complete manifest hash above binds the three paths, env-key semantics,
    # condition, wrapper and remote expression. Do not evaluate unrelated Swift.
    start = manifest.index(marker)
    remote = manifest.index("    : .binaryTarget(", start)
    end = manifest.index("\n\nlet package =", remote)
    return manifest[:start] + "let irohBinary: Target = " + manifest[remote + 6:end] + manifest[end:]


def binary_targets(manifest, product, *, checkout=None, bindings=None):
    manifest = selected_manifest(manifest, product, checkout, bindings)
    # Remove comments without touching URL strings. Computed expressions are not
    # treated as evidence. Literal constants/interpolation cover release manifests.
    text = re.sub(r'("(?:[^"\\]|\\.)*")|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda m: m.group(1) or " ", manifest)
    constants = dict(re.findall(r'(?:let|var)\s+(\w+)\s*(?::\s*String\s*)?=\s*("(?:[^"\\]|\\.)*")', text))

    def value(expression, active=()):
        expression = expression.strip()
        require(expression not in active, "cyclic manifest constant")
        if expression in constants:
            return value(constants[expression], (*active, expression))
        require(re.fullmatch(r'"(?:[^"\\]|\\.)*"', expression) is not None,
                "unsupported computed manifest string: " + expression[:80])
        expression = re.sub(r'\\\((\w+)\)', lambda m: value(m.group(1), active), expression)
        return json.loads(expression)

    binaries = {}
    for target in calls(text, "binaryTarget"):
        require(set(target) == {"name", "url", "checksum"}, "unsupported local/computed binary target")
        target = {key: value(val) for key, val in target.items()}
        require(re.fullmatch(r"[0-9a-f]{64}", target["checksum"]), "invalid provider checksum")
        require(target["url"].startswith("https://"), "non-HTTPS provider distribution")
        require(target["name"] not in binaries, "ambiguous provider binary target")
        binaries[target["name"]] = target
    libraries = [x for x in calls(text, "library") if value(x["name"]) == product]
    require(len(libraries) == 1, "unproven/ambiguous selected product " + product)
    targets = json.loads(libraries[0]["targets"])
    wrappers = {value(t["name"]): t for t in calls(text, "target") if "name" in t}
    selected, active = {}, set()

    def visit(name):
        require(name not in active, "cyclic selected target graph")
        if name in binaries:
            selected[name] = binaries[name]
            return
        require(name in wrappers, "unproven selected target " + name)
        active.add(name)
        deps = wrappers[name].get("dependencies", "[]")
        # Support literal strings and the exact Swift .byName(name: "Iroh")
        # form used by the pinned wrapper; conditional/external/dynamic forms
        # still fail closed rather than silently dropping a selected edge.
        require(deps.startswith("[") and deps.endswith("]"),
                "unsupported selected target dependency configuration")
        expressions = split_args(deps[1:-1])
        if expressions and not expressions[-1]:
            expressions.pop()  # Swift's optional trailing array comma.
        for expression in expressions:
            by_name = re.fullmatch(r'\.byName\(\s*name\s*:\s*("(?:[^"\\]|\\.)*")\s*\)', expression)
            literal = by_name.group(1) if by_name else expression
            require(re.fullmatch(r'"(?:[^"\\]|\\.)*"', literal) is not None,
                    "unsupported selected target dependency configuration")
            try:
                dependency = json.loads(literal)
            except json.JSONDecodeError:
                raise ValueError("Provider contract: unsupported selected target dependency configuration") from None
            visit(dependency)
        active.remove(name)
    for target in targets:
        visit(target)
    require(selected, "selected binary provider has no proven binary distribution")
    return list(selected.values())


def macho_content(data):
    """Compare executable content across CodeSignOnCopy, not signature identity.

    Only LC_CODE_SIGNATURE, its terminal blob, and __LINKEDIT signature-dependent
    sizes are normalized. All other load commands/sections/bytes remain bound.
    Fat slice tables are canonicalized by CPU/subtype, with every slice included.
    Unsupported formats fail closed at the ordinary byte hash, never exempted.
    """
    if data[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        wide = data[3] == 0xbf
        require(len(data) >= 8, "short fat distribution header")
        count = struct.unpack_from(">I", data, 4)[0]
        require(0 < count <= 32, "invalid fat distribution image")
        slices = {}
        for i in range(count):
            fmt = ">IIQQII" if wide else ">IIIII"
            size = struct.calcsize(fmt)
            require(8 + count * size <= len(data), "short fat distribution table")
            fields = struct.unpack_from(fmt, data, 8 + i * size)
            cpu, subtype, offset, length = fields[:4]
            require(offset + length <= len(data) and length > 0, "invalid fat slice extent")
            key = f"{cpu}:{subtype}"
            require(key not in slices, "duplicate fat slice")
            slices[key] = macho_content(data[offset:offset + length])
        return {"fat": slices}
    if data[:4] != b"\xcf\xfa\xed\xfe":
        return {"sha256": digest(data)}
    require(len(data) >= 32, "short Mach-O distribution member")
    header = bytearray(data[:32])
    count, size = struct.unpack_from("<II", data, 16)
    require(count <= 4096 and 32 + size <= len(data), "invalid Mach-O commands")
    position, commands, signature = 32, [], None
    for _ in range(count):
        require(position + 8 <= 32 + size, "short Mach-O load command")
        cmd, length = struct.unpack_from("<II", data, position)
        require(length >= 8 and position + length <= 32 + size, "invalid Mach-O command extent")
        command = bytearray(data[position:position + length])
        if cmd == 0x1d:
            require(length == 16 and signature is None, "ambiguous code signature")
            offset, length_sig = struct.unpack_from("<II", command, 8)
            require(offset >= 32 + size and offset + length_sig == len(data), "nonterminal signature")
            signature = offset
        else:
            if cmd == 0x19 and command[8:24].rstrip(b"\0") == b"__LINKEDIT":
                require(length >= 72, "short linkedit command")
                struct.pack_into("<Q", command, 32, 0)  # vmsize includes signing allocation
                struct.pack_into("<Q", command, 48, 0)  # filesize includes signature blob
            commands.append(bytes(command))
        position += length
    require(position == 32 + size, "unparsed Mach-O load command bytes")
    struct.pack_into("<II", header, 16, len(commands), sum(map(len, commands)))
    return {"machoContentSHA256": digest(bytes(header) + b"".join(commands) + data[32 + size:signature])}


def static_archive(data):
    if data.startswith(b"!<arch>\n"):
        return True
    if data[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        wide = data[3] == 0xbf
        require(len(data) >= 8, "short fat archive header")
        count = struct.unpack_from(">I", data, 4)[0]
        require(0 < count <= 32, "invalid fat archive")
        slices = []
        for i in range(count):
            fmt = ">IIQQII" if wide else ">IIIII"
            require(8 + count * struct.calcsize(fmt) <= len(data), "short fat static archive table")
            fields = struct.unpack_from(fmt, data, 8 + i * struct.calcsize(fmt))
            offset, length = fields[2:4]
            require(offset + length <= len(data) and length > 0, "invalid fat archive slice")
            slices.append(data[offset:offset + length].startswith(b"!<arch>\n"))
        return all(slices)
    return False


def distribution(archive, target):
    """Derive REQUIRED runtime members from the genuine selected macOS slice."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        members, total = {}, 0
        for info in z.infolist():
            name = info.filename.rstrip("/")
            parts = PurePosixPath(name).parts
            require(name and not name.startswith("/") and ".." not in parts and "\\" not in name,
                    "unsafe distribution member")
            require(name not in members, "duplicate distribution member")
            total += info.file_size
            require(len(members) < 40000 and total <= MAX_BYTES, "distribution exceeds bounded inventory")
            members[name] = info
        roots = [n for n in members if n.endswith(".xcframework/Info.plist") and n.count("/") == 1]
        require(len(roots) == 1, "unsupported archive root layout")
        root = str(PurePosixPath(roots[0]).parent)
        info = plistlib.loads(z.read(members[roots[0]]))
        slices = [s for s in info["AvailableLibraries"] if s.get("SupportedPlatform") == "macos" and
                  not s.get("SupportedPlatformVariant") and "x86_64" in s.get("SupportedArchitectures", [])]
        require(len(slices) == 1, "no unique Intel macOS provider distribution")
        selected = slices[0]
        identifier, library = selected["LibraryIdentifier"], selected["LibraryPath"]
        require(PurePosixPath(identifier).name == identifier and PurePosixPath(library).name == library,
                "unsupported selected distribution path")
        prefix = f"{root}/{identifier}/{library}"
        runtime = {}
        static = library.endswith(".a")
        if static:
            require(static_archive(z.read(members[prefix])), "unproven static archive")
        else:
            require(library.endswith(".framework"), "unsupported binary distribution kind")
            # Static XCFramework framework slices are link inputs, not runtime helpers.
            executables = [n for n in members if n.startswith(prefix + "/") and
                           PurePosixPath(n).name == library[:-10] and
                           not stat.S_ISLNK(members[n].external_attr >> 16)]
            require(len(executables) == 1, "unproven framework executable")
            static = static_archive(z.read(members[executables[0]]))
            if not static:
                for name, member in members.items():
                    if not name.startswith(prefix + "/"):
                        continue
                    relative = name[len(prefix) + 1:]
                    parts = PurePosixPath(relative).parts
                    if development_member(relative):
                        continue  # Outer framework development headers removed by embed configuration.
                    mode = member.external_attr >> 16
                    if member.is_dir():
                        runtime[relative] = {"type": "directory"}
                    elif stat.S_ISLNK(mode):
                        link = z.read(member).decode()
                        require(not link.startswith("/"), "absolute provider symlink")
                        # Lexical resolution rejects escape, including nested .. chains.
                        stack = list(PurePosixPath(relative).parent.parts)
                        for part in PurePosixPath(link).parts:
                            if part == "..":
                                require(stack, "escaping provider symlink")
                                stack.pop()
                            elif part != ".":
                                stack.append(part)
                        runtime[relative] = {"type": "symlink", "target": link}
                    else:
                        require(stat.S_ISREG(mode) or stat.S_IFMT(mode) == 0, "special provider member")
                        runtime[relative] = ({"type": "signature"} if signature_member(relative) else
                                             {"type": "file", "content": macho_content(z.read(member))})
                require(runtime, "empty provider runtime distribution")
        return {"target": target, "selectedSlice": selected, "library": library,
                "condition": "macOS x86_64 / stock Debug selected product; static link input" if static else
                             "macOS x86_64 / stock Debug selected product; required embedded runtime",
                "runtimeRequired": not static, "requiredMembers": runtime}


def derive(source, packages, archives, bindings=None):
    # Collect passes its owner across BOTH derivations. Standalone derivation
    # owns the same lifetime rather than falling back to per-read protection.
    if bindings is not None:
        return _derive(source, packages, archives, bindings)
    with _InputBindings() as owned:
        return _derive(source, packages, archives, owned)


def _derive(source, packages, archives, bindings):
    source, packages = bindings.directory(source), bindings.directory(packages)
    archives = sorted({str(Path(path).resolve()) for path in archives})
    source_files, pins = source_proof(source, bindings)
    results, archive_index, scanned_bytes = [], {}, 0
    for archive_path in archives:
        data, proof = bindings.read(archive_path)
        scanned_bytes += len(data)
        require(scanned_bytes <= 4 * MAX_BYTES, "total cached archive byte bound exceeded")
        archive_index.setdefault(proof["sha256"], []).append(proof)
    # Fence the listing root before discovery; bind each selected checkout
    # before its first metadata/Git use. Unrelated checkouts are not inputs.
    checkouts = bindings.directory(packages / "checkouts")
    directories = list(checkouts.iterdir())
    selected_checkouts = {}
    for identity in PRODUCTS:
        require(identity in pins and pins[identity]["kind"] == "remoteSourceControl", "missing/wrong locked provider")
        candidates = [p for p in directories if p.name.lower() == identity]
        require(len(candidates) == 1, "missing/ambiguous locked provider checkout " + identity)
        checkout = candidates[0]
        bindings.directory(checkout)
        require(checkout.is_dir() and not checkout.is_symlink(), "nonphysical locked provider checkout " + identity)
        selected_checkouts[identity] = checkout
    for identity, product in PRODUCTS.items():
        pin, checkout = pins[identity], selected_checkouts[identity]
        blob = git_blob(checkout, pin["state"]["revision"], "Package.swift")
        physical, manifest_file = bindings.read(checkout / "Package.swift")
        require(blob == physical, "provider working manifest differs from locked Git object")
        targets = binary_targets(blob.decode(), product, checkout=checkout, bindings=bindings)
        for target in targets:
            candidates = archive_index.get(target["checksum"], [])
            require(len(candidates) == 1, "missing/ambiguous checksum-verified archive for " + identity + "/" + target["name"])
            archive = candidates[0]
            data, current = bindings.read(archive["path"])
            require(current == archive, "provider archive changed during derivation")
            results.append({"identity": identity, "lock": pin, "product": product,
                            "manifest": manifest_file, "manifestGitSHA256": digest(blob),
                            "binaryTarget": target, "archive": archive,
                            "distribution": distribution(data, target["name"])})
    import native_cmux as native
    return {"schema": 1, "sourceCommit": native.SOURCE_SHA, "sourceTree": native.SOURCE_TREE,
            "configuration": "Debug", "platform": "macos", "architecture": "x86_64",
            "source": str(source), "packages": str(packages), "archives": [str(Path(p).resolve()) for p in archives],
            "sourceFiles": source_files, "providers": results}


def source_owned_images():
    import native_cmux as native
    return set(native.STOCK_PRODUCTS)


def verify(app, reference, payload=True):
    """Receipt alone is never proof: rederive and compare physical inputs."""
    with _InputBindings() as bindings:
        return _verify(app, reference, payload, bindings)


def _verify(app, reference, payload, bindings):
    reference = Path(reference)
    raw, receipt_identity = bindings.read(reference)
    contract = json.loads(raw)
    actual = derive(contract["source"], contract["packages"], contract["archives"], bindings)
    require(actual == contract, "provider/source identity/configuration/distribution changed since collection")
    if not payload:
        return {"reference": receipt_identity, "contract": actual}
    covered = []
    for provider in actual["providers"]:
        dist = provider["distribution"]
        if not dist["runtimeRequired"]:
            require(not (app / "Contents/Frameworks" / dist["library"]).exists(), "unexpected embedded static provider")
            continue
        root = app / "Contents/Frameworks" / dist["library"]
        require(root.is_dir() and not root.is_symlink(), "missing required provider " + dist["library"])
        covered.append(root)
        for relative, expected in dist["requiredMembers"].items():
            path = root / relative
            require(path.exists() or path.is_symlink(), "missing required nested provider member " + relative)
            mode = path.lstat().st_mode
            if expected["type"] == "symlink":
                require(stat.S_ISLNK(mode) and os.readlink(path) == expected["target"], "provider symlink mismatch")
            elif expected["type"] == "directory":
                require(stat.S_ISDIR(mode), "provider directory mismatch")
            else:
                data, _ = bindings.read(path)
                # CodeSignOnCopy may regenerate signature resources. Presence,
                # regular/no-hardlink identity and final ZIP bytes remain mandatory.
                if expected["type"] != "signature":
                    require(macho_content(data) == expected["content"], "provider member content mismatch " + relative)
        for path in root.rglob("*"):
            relative = str(path.relative_to(root))
            if (path.is_dir() and not path.is_symlink()) or development_member(relative) or signature_member(relative):
                continue
            require(relative in dist["requiredMembers"], "unproven extra provider runtime member " + relative)
    # Source-built/dlopen stock Nucleo is separately source-bound. Any other
    # embedded provider without a supported verified distribution is a blocker.
    for path in app.rglob("*"):
        if path.is_symlink() or not (path.suffix == ".framework" or path.suffix == ".dylib"):
            continue
        if str(path.relative_to(app)) in source_owned_images():
            continue
        require(any(path == root or root in path.parents for root in covered), "unsupported/unproven embedded provider " + str(path.relative_to(app)))
    return {"reference": receipt_identity, "contract": actual}


def cached_archives(cache_roots, fences):
    """Discover ZIP bytes, not SwiftPM cache basenames (URL keys can end _zip).

    Walk physical directories via descriptors; never follow cache aliases. ZIP
    probing is bounded and does not extract anything. Only derive's locked
    manifest SHA256/Git/member verification can make a candidate authoritative.
    Keep candidate ancestor fences alive until both derivations have finished.
    """
    import native_cmux as native
    archives, entries, archive_bytes, probe_bytes = {}, 0, 0, 0
    fenced_directories = set()

    def traversal_error(error):
        raise error

    for root in cache_roots:
        root = Path(root).absolute()
        require(not root.is_symlink(), "symlink cache root")
        if not root.exists():
            continue
        require(root == root.resolve(strict=True) and root.is_dir(), "nonphysical cache root/ancestor")
        if root not in fenced_directories:
            fences.enter_context(directory_fence(root))
            fenced_directories.add(root)
        for directory, directories, files, directory_fd in os.fwalk(root, follow_symlinks=False, onerror=traversal_error):
            directory = Path(directory)
            entries += len(directories) + len(files)
            require(entries <= 40000, "archive cache traversal bound exceeded")
            require(directory == directory.resolve(strict=True), "symlink cache ancestor")
            with directory_fence(directory):
                require(native.file_identity(os.fstat(directory_fd)) == native.file_identity(directory.stat(follow_symlinks=False)),
                        "cache directory identity changed")
                for name in files:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    path = directory / name
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        require(stat.S_ISREG(before.st_mode) and native.file_identity(before) == native.file_identity(info),
                                "cache file identity changed")
                        # is_zipfile probes the final <=64KiB EOCD region plus
                        # fixed EOCD/ZIP64 records, never unbounded file content.
                        probe_bytes += min(before.st_size, 65558) + 128
                        require(probe_bytes <= 4 * MAX_BYTES, "cache ZIP probe byte bound exceeded")
                        is_zip = zipfile.is_zipfile(stream)
                        require(native.file_identity(os.fstat(stream.fileno())) == native.file_identity(before) and
                                native.file_identity(path.stat(follow_symlinks=False)) == native.file_identity(before),
                                "cache file changed during ZIP probe")
                    if not is_zip:
                        continue
                    require(before.st_nlink == 1 and before.st_size <= MAX_BYTES,
                            "archive must be a bounded, single-link regular file")
                    if directory not in fenced_directories:
                        fences.enter_context(directory_fence(path))
                        fenced_directories.add(directory)
                    require(native.file_identity(path.stat(follow_symlinks=False)) == native.file_identity(before),
                            "cache archive pathname changed")
                    if str(path) not in archives:
                        archives[str(path)] = {"identity": native.file_identity(before), "bytes": before.st_size}
                        archive_bytes += before.st_size
                    require(len(archives) <= 256, "archive cache discovery bound exceeded")
                    require(archive_bytes <= 4 * MAX_BYTES, "total cached archive byte bound exceeded")
    return archives


def collect(source, packages, cache_roots, output):
    with _InputBindings() as fences:
        # Source/package roots can be disjoint from every cache root. Acquire
        # them before discovery and retain all later input bindings on this
        # owner through the second derivation and the pre-publication seal.
        source, packages = fences.directory(source), fences.directory(packages)
        archives = cached_archives(cache_roots, fences)
        require(archives, "no cached distribution archives after stock build; no download/fallback")
        # Hash all discovered ZIPs; manifest-selected SHA256, source/provider Git
        # identity and independent distribution-member checks remain authority.
        first = derive(source, packages, archives, fences)
        selected = {p["archive"]["path"]: p["archive"] for p in first["providers"]}
        for path, proof in selected.items():
            require(proof["identity"] == archives[path]["identity"] and proof["bytes"] == archives[path]["bytes"],
                    "provider archive changed since cache discovery")
        # Retain ONLY locked distributions, not unrelated cache inputs. Recheck
        # physical/hash identity across both derivations, then close fences BEFORE
        # publishing any contract (an ancestor ABA must leave no proof behind).
        contract = derive(source, packages, sorted(selected), fences)
        require({p["archive"]["path"]: p["archive"] for p in contract["providers"]} == selected,
                "provider archive changed during collection")
    with Path(output).open("x") as stream:
        json.dump(contract, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--derived-data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    packages = args.derived_data / "SourcePackages"
    report = {"ok": False, "mode": "locked-provider-collection", "network": False,
              "buildOrLaunchPerformedByCollector": False, "runtimeCompatibility": "UNPROVEN"}
    try:
        contract = collect(args.source, packages,
                           [packages / "artifacts", Path.home() / "Library/Caches/org.swift.swiftpm"], args.output)
        report.update(ok=True, providers=[p["identity"] for p in contract["providers"]])
    except (ValueError, OSError, KeyError, TypeError, struct.error, zipfile.BadZipFile,
            plistlib.InvalidFileException) as exc:
        report["error"] = str(exc)
    with args.output.with_name("providers-collection.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print("Locked provider collection: " + ("PASS (not runtime/API acceptance)" if report["ok"] else "FAIL CLOSED"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
