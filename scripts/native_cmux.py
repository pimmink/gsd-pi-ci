#!/usr/bin/env python3
"""D014/D015 isolated-CI preparation and non-executing artifact checks (stdlib only)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import tempfile
import zipfile
import subprocess
import sys
from native_cmux_fence import directory_fence, fenced

SOURCE_REPO = "pimmink/cmux"
SOURCE_REF = "fix/gsdpi-vault-resume-cas-20261110"
SOURCE_SHA = "33daf63d0152d5c7f26155506f0beb191e5ad3da"
SOURCE_TREE = "163b044d6d3b332e6e8a22b303c21960a159058d"
BUN_VERSION = "1.3.6"  # Same exact version as the pinned source's ci-macos.yml.
SOURCE_FILES = {
    "scripts/reload.sh": "a2690406a58bb5d0c7fb19fa2c9cd33f9a6ea4e7776f77b236483c2821b32a64",
    "scripts/lib/mobile-attach.sh": "d0088440836ba823b81c9ca508ee427a34c43fbb613e6aea4b4481471e86d5aa",
    "scripts/lib/dev-secrets.sh": "2304b8912292418accae561e7ce108e3dbfe07039cac92041c1c5baebae497f0",
    "scripts/lib/tagged-reload-lock.py": "efc0afcf35728ceb7b042481e6ef006c58302ab60ca340ec8dff4f2112feea53",
    "scripts/ghostty-zig-version.sh": "8f5ba64eafc9ad7f56a5c1f2d3e4d0df3fac46be7b590a3fe934874f460e2c9e",
    "scripts/install-zig-ci.sh": "b28cb0161f277bd1013490e2333f90cbb34fa2472d432d36583787024b20dcd4",
    "scripts/lib/dev-backend-origin.sh": "8e0daa0bee6de877047ca94e16c67475dad2151b16cbbdf14e39d56fedcab664",
    "scripts/ensure-ghosttykit.sh": "60aed22f75a9bfd110ecc2d36985420cf41c2dda885be43188f3b7e827ff0b8e",
    "scripts/build-ghostty-cli-helper.sh": "544d8121cf9ac3a4e936856929d5611793352757fd328b87fc505026f37eeb29",
    "scripts/build-cmux-cua.sh": "32c8533dc34e6c632225b5b004351e86d251ee5fffe4a9f5b84db1972fccf8c4",
    "scripts/ci/resolve-cmux-tui-client-commit.sh": "0d5e170659d751c6fc85ed011cd33a3a9bd14f8676beaadd37983d4d936b81ef",
    "scripts/install-cmux-tui-client.sh": "5e0ec3fdb98c15a85749dc05c5233fb1a640eac963774b2c450efd79e3e89d76",
    "scripts/install-coderouter-client.sh": "65e1757e3622e2dc5b32ac5f549a3a04b28ec8cbc7d3ccb1d8b2d062fa442c35",
    "scripts/local-build-cache-preflight.py": "8260e9f398effc0164cc2b1511c6f51976c41542428e17ff5c10bfe474ca057a",
    "scripts/ci/swift_incremental_diagnostics.py": "87a58eef5b22df7b60fd1065401aadbb3d7a21430b1d09e2461ec1bf17fb1e66",
    "cmux.xcodeproj/project.pbxproj": "c2ce7b22d2bb1f7746a5fd4483bd78d12846335a836972bee8fd1878f8f7e2fe",
    # D019 source-bound product/condition/provenance evidence, retained in receipts.
    ".gitmodules": "82fb16091e4581e37ae0a72f0d14c408e46cc9a582fe6f63d399ecd8477fb46a",
    "Resources/Info.plist": "35aa6979fbc6f69e474a3e2f3e30ac4b409d50eaee061de8b4c3a0088ce39c14",
    "TunnelExtension/Info.plist": "a8d2a3baac1d12726e1d181f646894a5e34f58a199ac84b2127d4c1b9ccb7177",
    "scripts/build-app-bundled-resources.sh": "3a32dc6be520e811f232ec3f3b7d6822fb16aae73b053ba01f70416ea6805378",
    "scripts/build-plain-text-paste-worker.sh": "c6886cd4ed0823d7c0a4e24e4e651e41273395713936300ae5d9850d8d51de7d",
    "scripts/build-diff-sidecar.sh": "12cbcb12b78edfe2f3e8fe9361074fc73fc0b6e754aa15d284550a315a694b6e",
    "scripts/build-command-palette-nucleo-ffi.sh": "2928ee6d05f845cf7c30a9b7380c74e179d714727ba29458deae5692411f0325",
    "Packages/macOS/CmuxCommandPalette/Sources/CmuxCommandPalette/Search/Nucleo/CommandPaletteNucleoSearchLibrary.swift": "33bafafb149ddd0b23f3287da9e93e5b1a36938d736ea4e11bcc1c40b53295ef",
    "cmux.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved": "e85b10d8e60e9c87adef48b32a1cfd2a5f89008371cd2f67b370c245e7ad5082",
    ".github/workflows/ci-macos.yml": "3e8e0f10811c5939ec4f99f07522a3aa595d7f74727c1d57a91ee07cc072afa5",
}
CI_REPO = "pimmink/gsd-pi-ci"
WORKFLOW = ".github/workflows/native-cmux-build-only.yml"
TARGET_HOST = "15.7.9"  # Actual Intel operator host, NOT the macos-26-intel build host.
# Exact Debug/tagged stock configuration at SOURCE_SHA. No caller allow-missing list.
# reload.sh:2068-2096 makes cmuxd conditional; git ls-tree at SOURCE_SHA has NO cmuxd.
STOCK_PRODUCTS = {
    "Contents/Resources/bin/cmux": "EXECUTE",  # project.pbxproj:4846-4854,14560-14594
    "Contents/Resources/bin/ghostty": "EXECUTE",  # build-app-bundled-resources.sh:293-317
    "Contents/Resources/bin/cmux-tui": "EXECUTE",  # reload.sh:2098-2134
    "Contents/Resources/bin/cmux-paste-text-worker": "EXECUTE",  # project.pbxproj:15095-15107
    "Contents/Resources/bin/cmux-diff-sidecar": "EXECUTE",  # build-diff-sidecar.sh:65-99
    "Contents/Library/cmux Computer Use.app/Contents/MacOS/cmux-cua": "EXECUTE",  # build-cmux-cua.sh:556-607
    "Contents/Frameworks/libcmux_command_palette_nucleo_ffi.dylib": "DYLIB",  # build-command-palette-nucleo-ffi.sh:55-121; explicit dlopen
}
STOCK_BUNDLES = {
    "Contents/PlugIns/CmuxDockTilePlugin.plugin": ("CmuxDockTilePlugin", "BUNDLE"),  # project.pbxproj:4873-4882,20059-20075; Info.plist:106-107
    "Contents/Library/SystemExtensions/cmuxTunnel.systemextension": ("cmuxTunnel", "EXECUTE"),  # project.pbxproj:4884-4893,20153-20169
    "Contents/Frameworks/Sparkle.framework": ("Sparkle", "DYLIB"),  # project.pbxproj:3727,9562; Package.resolved: Sparkle 2.9.5
}
DOCK_HOST = Path("/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock")
MAX_DYLD_DEPTH = 64
MAX_DYLD_CONTEXTS = 4096
MAX_DYLD_EDGES = 65536
REMOTE_PREFIX = "Contents/Resources/bin/cmux-tui-ssh/"
# install-cmux-tui-client.sh:301-317: exact names, mode 0644, manifest SHA256.
REMOTE_NAMES = {"cmux-tui-" + target for target in (
    "aarch64-apple-darwin", "x86_64-apple-darwin",
    "aarch64-unknown-linux-musl", "x86_64-unknown-linux-musl")}
MAX_UPLOAD = 400 * 1024 * 1024
MACH_MAGIC = {bytes.fromhex(x) for x in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def version(text):
    require(isinstance(text, str) and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,2}", text),
            f"Unparseable version: {text!r}")
    values = tuple(map(int, text.split(".")))
    return values + (0,) * (3 - len(values))


def tag_ok(tag):
    # No spaces, shell metacharacters, shared/default tags, or source-selected paths.
    return (isinstance(tag, str) and bool(re.fullmatch(r"d014-[a-z0-9][a-z0-9-]{9,59}", tag))
            and len(tag) <= 64 and "--" not in tag and not tag.endswith("-"))  # Stock slug/cloud limit.


def run(*args, cwd=None, env=None, timeout=180):
    try:
        return subprocess.check_output(args, cwd=cwd, env=env, text=True, stderr=subprocess.PIPE, timeout=timeout).rstrip("\n")
    except subprocess.CalledProcessError as exc:
        # Commands contain no credentials; never echo external stderr or the environment.
        raise ValueError(f"Tool failed (exit {exc.returncode}): {' '.join(args)}") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"Tool timed out: {args[0]}; no retry/fallback") from None


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def architectures(text):
    text = text.strip()
    if " are: " in text:
        text = text.rsplit(" are: ", 1)[1]
    elif " is architecture: " in text:
        text = text.rsplit(" is architecture: ", 1)[1]
    values = text.split()
    require(values and all(re.fullmatch(r"(?:x86_64h?|arm64e?|i386|armv7s?)", a) for a in values),
            "Unparseable lipo architecture output")
    require("x86_64" in values, "Missing required x86_64 slice")
    return values


def x86_output(text):
    """Select the explicitly requested slice even if tool output includes a fat header."""
    sections = re.split(r"(?m)^[^\n]*\(architecture ([^)]+)\):\s*$", text)
    if len(sections) > 1:
        matches = [sections[i + 1] for i in range(1, len(sections), 2) if sections[i] == "x86_64"]
        require(len(matches) == 1, "Missing/ambiguous x86_64 tool output")
        return matches[0]
    return text


def file_type(text):
    lines = x86_output(text).splitlines()
    for i, line in enumerate(lines[:-1]):
        columns = line.split()
        if columns and columns[0] == "magic" and "filetype" in columns:
            row = lines[i + 1].split()
            index = columns.index("filetype")
            require(len(row) > index and "cputype" in columns, "Malformed Mach-O header")
            require(len(row) > columns.index("cputype") and row[columns.index("cputype")] == "X86_64",
                    "Requested Mach-O header is not x86_64")
            kind = row[index].removeprefix("MH_")
            require(kind in ("EXECUTE", "DYLIB", "BUNDLE"), f"Unexpected embedded Mach-O file type: {kind}")
            return kind
    raise ValueError("Missing Mach-O filetype header")


def load_commands(text):
    """Parse only x86_64 `otool -arch x86_64 -l`; never infer minOS from SDK."""
    minimums, rpaths, dependencies, dylib_loads = [], [], [], []
    for block in re.split(r"(?m)^Load command \d+\s*$", x86_output(text))[1:]:
        match = re.search(r"(?m)^\s*cmd (LC_\w+)\s*$", block)
        require(match, "Malformed load command")
        cmd = match[1]
        if cmd in ("LC_BUILD_VERSION", "LC_VERSION_MIN_MACOSX"):
            if cmd == "LC_BUILD_VERSION":
                platform = re.search(r"(?m)^\s*platform (\S+)\s*$", block)
                require(platform and platform[1] in ("1", "MACOS", "macos"), "Non-macOS or missing platform")
            key = "minos" if cmd == "LC_BUILD_VERSION" else "version"
            v = re.search(rf"(?m)^\s*{key} (\S+)\s*$", block)
            require(v, "Missing Mach-O minimum OS")
            version(v[1])
            minimums.append(v[1])
        elif cmd == "LC_RPATH":
            m = re.search(r"(?m)^\s*path (.+) \(offset \d+\)\s*$", block)
            require(m, "Unparseable LC_RPATH")
            rpaths.append(m[1])
        elif cmd in ("LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB"):
            m = re.search(r"(?m)^\s*name (.+) \(offset \d+\)\s*$", block)
            require(m, "Unparseable dylib dependency")
            dependencies.append(m[1])
            dylib_loads.append({"command": cmd, "name": m[1]})
        elif cmd == "LC_DYLD_ENVIRONMENT":
            raise ValueError("Unexpected embedded dyld environment")
    require(len(minimums) == 1, "Missing or ambiguous x86_64 minimum OS load command")
    require(version(minimums[0]) <= version(TARGET_HOST), f"x86_64 minOS {minimums[0]} exceeds {TARGET_HOST}")
    return {"minimum_os": minimums[0], "rpaths": rpaths, "dependencies": dependencies,
            "dylib_loads": dylib_loads}


def system_path(path):
    # Only Apple system load locations. /usr/local, /opt, /Library and developer SDKs are forbidden.
    return path.startswith(("/System/Library/", "/usr/lib/")) and ".." not in Path(path).parts


def inside(path, root):
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def physical_path(path, root):
    """Resolve only relative internal links; reject even an external-then-internal bounce.

    This is path validation, not a substitute for the descriptor/identity snapshot.
    Missing final paths are allowed here so weak and strong loads can differ later.
    """
    require(path.is_absolute() and path.is_relative_to(root), f"Path escapes bundle: {path}")
    pending, cursor, links = list(path.relative_to(root).parts), root, 0
    steps = 0
    while pending:
        steps += 1
        require(steps <= 512, "Physical path resolution bound exceeded")
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            require(cursor != root, f"Path escapes bundle: {path}")
            cursor = cursor.parent
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            links += 1
            require(links <= 32, "Cyclic/excessive bundle symlink chain")
            target = Path(os.readlink(cursor))
            require(not target.is_absolute(), f"External/non-relocatable bundle symlink: {path}")
            cursor = cursor.parent
            pending = list(target.parts) + pending
    return cursor


def expand_bundle_path(value, loader, executable, app):
    for prefix, root in (("@loader_path", loader), ("@executable_path", executable)):
        if value == prefix or value.startswith(prefix + "/"):
            suffix = value[len(prefix):]
            require(not suffix.startswith("//") and "\x00" not in suffix and len(value) <= 4096,
                    f"Invalid bundle-relative path: {value}")
            path = root / suffix.lstrip("/")
            if root.is_relative_to(app):
                return physical_path(path, app)
            # Only the source-proven Dock host may anchor a plugin outside this app.
            require(executable == DOCK_HOST.parent and prefix == "@executable_path",
                    f"Unproven external executable anchor: {value}")
            path = Path(os.path.normpath(path))
            require(system_path(str(path)), f"Unbundled/unsupported Dock path: {value}")
            return path
    raise ValueError(f"Unbundled/unsupported path: {value}")


def system_stub(dependency, sdk, weak=False):
    require(system_path(dependency), f"Not an Apple system dependency: {dependency}")
    path = sdk / dependency.lstrip("/")
    stub = path.with_suffix(".tbd") if path.suffix == ".dylib" else path.with_name(path.name + ".tbd")
    stub = physical_path(stub, sdk)
    if weak and not stub.exists():
        return {"status": "weak-absent", "availability_fallback_proof": "UNPROVEN"}
    require(stub.is_file(), f"Missing SDK system dependency stub: {dependency}")
    require(stub.stat().st_size <= 4 * 1024 * 1024, "Unexpectedly large SDK stub")
    text = stub.read_text()
    x64 = re.search(r"\bx86_64-macos\b", text) or (
        re.search(r"\barchs\s*:\s*\[[^]]*\bx86_64\b[^]]*\]", text) and
        re.search(r"\bplatform\s*:\s*macosx\b", text))
    require(x64, f"SDK stub lacks x86_64 macOS target: {dependency}")
    return {"sdk_stub": str(stub.relative_to(sdk)), "sha256": sha256(stub),
            "evidence_scope": "selected SDK metadata only; target-host API/symbol/runtime UNPROVEN"}


def check_links(commands, image, main, app, images, search, sdk, promote):
    """Resolve in dyld search order for ONE actual executable/loader-stack context.

    Never skip a present wrong-type candidate in favour of a later good one. Weak
    absence is recorded, not claimed safe; unsafe weak paths still fail closed.
    """
    resolved = []
    for load in commands["dylib_loads"]:
        dep, kind = load["name"], load["command"]
        require(dep and len(dep) <= 4096 and "\x00" not in dep, "Invalid dependency path")
        weak = kind == "LC_LOAD_WEAK_DYLIB"
        result = {"dependency": dep, "load_command": kind}
        if system_path(dep):
            result.update(route="Apple system", **system_stub(dep, sdk, weak))
            resolved.append(result)
            continue
        if dep.startswith("@rpath/"):
            require(".." not in Path(dep).parts and dep != "@rpath/" and not dep.startswith("@rpath//"),
                    f"Traversal/invalid rpath dependency: {dep}")
            candidates = [p / dep[len("@rpath/"):] for p in search]
        else:
            candidates = [expand_bundle_path(dep, image.parent, main.parent, app)]
        found = False
        for candidate in candidates:
            if system_path(str(candidate)):
                # Probe metadata existence only. Malformed/present stubs are NOT ignored.
                evidence = system_stub(str(candidate), sdk, weak=True)
                if evidence.get("status") == "weak-absent":
                    continue
                result.update(route="Apple system", **evidence)
                found = True
                break
            candidate = physical_path(candidate, app)
            if not candidate.exists():
                continue
            require(candidate in images, f"Missing/non-Mach-O bundled dependency: {dep}")
            record = promote(candidate)  # A host edge overrides remote DATA classification.
            require(record["file_type"] == "DYLIB", f"{kind} requires MH_DYLIB, not {record['file_type']}: {dep}")
            result.update(route=str(candidate.relative_to(app)), image=str(candidate.relative_to(app)))
            found = True
            break
        if not found:
            require(weak, f"Unresolved bundled dependency: {dep}")
            result.update(status="weak-absent", availability_fallback_proof="UNPROVEN")
        resolved.append(result)
    return resolved


def plist_executable(plist, data, app):
    name = data.get("CFBundleExecutable")
    require(isinstance(name, str) and name and Path(name).name == name, f"Missing/invalid CFBundleExecutable: {plist.relative_to(app)}")
    if plist.parent.name == "Contents":
        path = plist.parent / "MacOS" / name
    elif plist.parent.name == "Resources":
        # Frameworks: Versions/A/Resources/Info.plist -> Versions/A/<executable>.
        path = plist.parent.parent / name
    else:
        path = plist.parent / name
    path = physical_path(path, app)
    require(path.is_file(), f"Missing required embedded executable: {plist.relative_to(app)}")
    require(path.stat().st_mode & 0o111, f"Required executable has no execute bit: {plist.relative_to(app)}")
    return path.resolve()


def file_identity(info):
    """Filesystem provenance, separate from the portable archive inventory."""
    # ctime cannot be restored by utime: detect transient same-inode writes and
    # restored pathname swaps around inspection tools as well as packaging reads.
    return {"device": info.st_dev, "inode": info.st_ino, "links": info.st_nlink,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def bundle_snapshot(app):
    """Freeze every entry, not just executables; never follow an external/link root."""
    require(not app.is_symlink(), "Bundle root cannot be a symlink")
    app = app.resolve()
    require(app.is_dir() and app.suffix == ".app", "Missing app bundle")
    root = app.stat()
    paths = [app] + bundle_paths(app)
    require(len(paths) <= 40001, "Unexpectedly large bundle inventory")
    inventory, files, directories, total = [], {}, {}, 0
    root_fd = os.open(app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require((os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) == (root.st_dev, root.st_ino),
                "Bundle root replaced during inventory")
        for p in paths:
            relative = str(p.relative_to(app))
            parent_fd = bundle_parent_fd(root_fd, relative)
            try:
                name = p.name if relative != "." else "."
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                item = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=parent_fd)
                    require(not Path(target).is_absolute() and physical_path(p, app).exists(),
                            f"External/dangling/non-relocatable bundle symlink: {relative}")
                    item.update(type="symlink", target=target)
                elif stat.S_ISDIR(info.st_mode):
                    directories[relative] = file_identity(info)
                    item.update(type="directory")
                else:
                    require(stat.S_ISREG(info.st_mode), f"Unsupported special bundle entry: {relative}")
                    require(info.st_nlink == 1, f"Bundle regular file has unsupported hardlink count: {relative}")
                    total += info.st_size
                    require(total <= 2 * 1024**3, "Bundle exceeds 2 GiB bound")
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        require(file_identity(opened) == file_identity(info) and
                                (opened.st_mode, opened.st_size) == (info.st_mode, info.st_size),
                                "Bundle entry identity/type/size replaced during inventory")
                        digest, count = hashlib.sha256(), 0
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            count += len(chunk)
                            require(count <= info.st_size, "Bundle entry grew during inventory")
                            digest.update(chunk)
                        after = os.fstat(stream.fileno())
                        require(count == info.st_size and file_identity(opened) == file_identity(after) and
                                (opened.st_mtime_ns, opened.st_ctime_ns) ==
                                (after.st_mtime_ns, after.st_ctime_ns), "Bundle entry changed during inventory")
                    files[relative] = file_identity(opened)
                    item.update(type="file", bytes=info.st_size, sha256=digest.hexdigest())
                inventory.append(item)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return {"identity": {"path": str(app), "device": root.st_dev, "inode": root.st_ino,
                         "files": files, "directories": directories},
            "inventory": inventory}


def verify_archive(archive, app_name, inventory):
    """Verify actual ZIP members/types/modes/bytes against the frozen FULL inventory."""
    expected = {(app_name if i["path"] == "." else app_name + "/" + i["path"]): i for i in inventory}
    seen = set()
    with zipfile.ZipFile(archive) as z:
        require(len(z.infolist()) == len(expected), "Archive inventory count changed")
        for entry in z.infolist():
            name = entry.filename.rstrip("/")
            require(name in expected and name not in seen and not entry.flag_bits & 1,
                    "Unexpected/duplicate/encrypted archive member")
            seen.add(name)
            item = expected[name]
            require(entry.filename == name + ("/" if item["type"] == "directory" else ""),
                    "Noncanonical archive member name")
            mode = entry.external_attr >> 16
            require(stat.S_IMODE(mode) == item["mode"], f"Archive mode changed: {name}")
            kind = item["type"]
            if kind == "directory":
                require(entry.is_dir() and stat.S_ISDIR(mode) and entry.file_size == 0, "Archive directory changed")
                continue
            require(not entry.is_dir(), "Archive member type changed")
            if kind == "symlink":
                target = item["target"].encode()
                require(stat.S_ISLNK(mode) and entry.file_size == len(target) and z.read(entry) == target,
                        "Archive symlink changed")
            else:
                require(stat.S_ISREG(mode) and entry.file_size == item["bytes"], "Archive file type/size changed")
                digest = hashlib.sha256()
                with z.open(entry) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                require(digest.hexdigest() == item["sha256"], "Archive file bytes changed")
        require(seen == set(expected), "Archive inventory incomplete")


def bundle_parent_fd(root_fd, relative):
    """Resolve each parent via an anchored directory fd, never through symlinks."""
    fd = os.dup(root_fd)
    try:
        for part in Path(relative).parent.parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def copy_inspected_bundle(app, staged, expected):
    """Copy frozen entries with NOFOLLOW on every source path component/open."""
    root_fd = os.open(app.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root = os.fstat(root_fd)
        require((root.st_dev, root.st_ino) == (expected["identity"]["device"], expected["identity"]["inode"]),
                "Bundle root replaced before private copy")
        staged.mkdir(mode=0o700)
        directories = []
        for item in expected["inventory"]:
            relative = item["path"]
            target = staged / relative
            if relative == ".":
                directories.append((target, item["mode"]))
                continue
            parent_fd = bundle_parent_fd(root_fd, relative)
            name = Path(relative).name
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                kind = item["type"]
                if kind == "directory":
                    # Even a same-content external directory replacement must not
                    # be traversed during the copy. Open/validate without following.
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    os.close(fd)
                    target.mkdir(mode=0o700)
                    directories.append((target, item["mode"]))
                elif kind == "symlink":
                    require(stat.S_ISLNK(info.st_mode) and os.readlink(name, dir_fd=parent_fd) == item["target"],
                            "Bundle symlink changed during copy")
                    target.symlink_to(item["target"])
                else:
                    identity = expected["identity"]["files"][relative]
                    require(info.st_nlink == 1 and file_identity(info) == identity,
                            f"Bundle file identity/hardlink count changed before copy: {relative}")
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                    with os.fdopen(fd, "rb") as source, target.open("xb") as dest:
                        before = os.fstat(source.fileno())
                        require(before.st_nlink == 1 and file_identity(before) == identity,
                                f"Bundle opened file identity/hardlink count changed during copy: {relative}")
                        require(stat.S_ISREG(before.st_mode) and before.st_size == item["bytes"] and
                                stat.S_IMODE(before.st_mode) == item["mode"], "Bundle file type/size/mode changed during copy")
                        digest = hashlib.sha256()
                        count = 0
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            count += len(chunk)
                            require(count <= item["bytes"], "Bundle file grew during copy")
                            dest.write(chunk)
                            digest.update(chunk)
                        after = os.fstat(source.fileno())
                        require(count == item["bytes"] and digest.hexdigest() == item["sha256"] and
                                file_identity(after) == identity and
                                (before.st_mtime_ns, before.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns),
                                "Bundle file changed during copy")
                    target.chmod(item["mode"])
            finally:
                os.close(parent_fd)
        for directory, mode in reversed(directories):
            directory.chmod(mode)
    finally:
        os.close(root_fd)


def package_app(app, output, report):
    # Only attempt-private paths are eligible for cleanup. Never check/unlink a
    # public pathname: even an inode match cannot make that operation atomic.
    with tempfile.TemporaryDirectory(prefix="cmux-package-", dir=output) as temp:
        archive = Path(temp) / "cmux-build-only.zip"
        _package_app(app, output, report, archive)
        # All fence exit validation has passed. Publish without overwriting;
        # nothing after this point destructively cleans a public pathname.
        os.link(archive, output / archive.name, follow_symlinks=False)


@fenced
def _package_app(app, output, report, archive):
    inspection = json.loads((output / "inspect.json").read_text())
    require(inspection.get("ok") is True and inspection.get("inventory_schema") == 1,
            "No passing full-inventory compatibility inspection; refusing app upload")
    expected = {"identity": inspection["bundle_identity"], "inventory": inspection["inventory"]}
    import native_cmux_providers as provider
    require("provider_proof" in inspection, "Missing verified locked-provider completeness proof")
    reference = inspection["provider_proof"]["reference"]["path"]
    require(provider.verify(app, reference) == inspection["provider_proof"],
            "Provider/source inputs changed since inspection")
    require(bundle_snapshot(app) == expected, "Bundle identity/inventory changed since inspection")
    require(not os.path.lexists(output / archive.name), "Archive already exists; no overwrite")
    # The archive producer never reads the mutable original. Check the private copy and
    # actual archive against the frozen inspection, then recheck both trees. No claims
    # about immutable OS flags or protection from an already-compromised runner.
    with tempfile.TemporaryDirectory(prefix="cmux-inspected-", dir=output) as temp:
        staged = Path(temp) / app.name
        copy_inspected_bundle(app, staged, expected)
        require(bundle_snapshot(staged)["inventory"] == expected["inventory"], "Private copy differs from inspection")
        require(provider.verify(staged, reference) == inspection["provider_proof"],
                "Private copy provider contract differs from inspection")
        require(bundle_snapshot(app) == expected, "Bundle changed during copy")
        # Resource forks/xattrs are not in this portable inventory; do not smuggle
        # uninspected __MACOSX/AppleDouble entries into an otherwise verified ZIP.
        run("ditto", "-c", "-k", "--norsrc", "--keepParent", str(staged), str(archive))
        require(archive.stat().st_size <= MAX_UPLOAD, "Archive exceeds upload cap")
        verify_archive(archive, app.name, expected["inventory"])
        require(bundle_snapshot(staged)["inventory"] == expected["inventory"], "Private copy changed during archive")
        require(bundle_snapshot(app) == expected, "Bundle changed during archive")
        require(provider.verify(app, reference) == inspection["provider_proof"],
                "Provider/source inputs changed during packaging")
        require(archive.stat().st_size + sum(p.stat().st_size for p in output.iterdir() if p.is_file())
                + 64 * 1024 <= MAX_UPLOAD, "Artifact exceeds total upload cap")
    report.update(archive=archive.name, bytes=archive.stat().st_size, sha256=sha256(archive),
                  archive_verified=True, inventory_schema=1, archive_metadata="portable files/modes/symlinks; no resource forks/xattrs")


def bundle_paths(app):
    paths = []
    for path in app.rglob("*"):
        require(len(paths) < 40000, "Unexpectedly large bundle file count")
        paths.append(path)
    return sorted(paths)


def remote_payloads(app, inventory):
    """No extension/prefix exemption: exact installer names + 0644 + manifest hash.

    This binds DATA classification, not publisher authenticity or host executability.
    Any actual host load edge promotes the image back to all host checks.
    """
    payloads = []
    for item in inventory:
        path = item["path"]
        if not path.startswith(REMOTE_PREFIX) or path[len(REMOTE_PREFIX):] not in REMOTE_NAMES:
            continue
        require(item["type"] == "file" and item["mode"] == 0o644, f"Invalid remote payload type/mode: {path}")
        manifest = app / REMOTE_PREFIX / "manifest.json"
        require(not manifest.is_symlink() and manifest.is_file() and manifest.stat().st_size <= 4 * 1024**2,
                "Missing/bounded physical remote payload manifest")
        data = json.loads(manifest.read_text())
        require(isinstance(data, dict) and re.fullmatch(r"[0-9a-f]{40}", str(data.get("commit", ""))),
                "Invalid remote payload manifest commit")
        binaries = data.get("binaries")
        require(isinstance(binaries, dict) and binaries.get(Path(path).name) == item["sha256"],
                f"Remote payload manifest digest mismatch: {path}")
        payloads.append({"path": path, "sha256": item["sha256"], "manifest_sha256": sha256(manifest),
                         "client_commit": data["commit"], "classification": "remote SSH DATA; not a host root",
                         "source": "scripts/install-cmux-tui-client.sh:301-317"})
    return payloads


@fenced
def inspect_app(app, report, execute=run, providers=None):
    import native_cmux_providers as provider
    providers = providers if providers is not None else app.parent / "native-cmux-providers.json"
    require(Path(providers).is_file(), "Missing required locked-provider distribution/configuration contract")
    provider_proof = provider.verify(app, providers, payload=False)
    before = bundle_snapshot(app)
    app = app.resolve()
    require(app.is_dir() and app.suffix == ".app", "Missing app bundle")
    sdk_path = Path(execute("xcrun", "--sdk", "macosx", "--show-sdk-path"))
    require(sdk_path.is_absolute(), "Invalid selected SDK path")
    sdk = sdk_path.resolve()
    require(sdk.is_dir(), "Missing selected macOS SDK for dependency validation")
    paths = bundle_paths(app)
    images, plists, anchors, bundle_exes, expected_types = {}, [], {}, {}, {}
    remote = remote_payloads(app, before["inventory"])
    remote_paths = {app / item["path"] for item in remote}
    report.update(target_host=TARGET_HOST, binaries=[], plists=plists, remote_payload_data=remote,
                  compatibility_scope="Static x86_64 inventory/minOS/dyld metadata only",
                  runtime_compatibility="UNPROVEN", source_commit=SOURCE_SHA, source_tree=SOURCE_TREE,
                  unresolved_obligations=["Target macOS 15.7.9 API/symbol/runtime and weak-availability/fallback proof",
                                          "Downloaded client immutable source-history pin/attestation provenance"])
    for relative in STOCK_BUNDLES:
        require((app / relative).is_dir(), f"Missing required source-defined bundle: {relative}")
    for bundle in [app] + [p for p in paths if p.is_dir() and not p.is_symlink() and p.suffix in (".app", ".framework", ".xpc", ".appex", ".plugin", ".systemextension")]:
        options = [bundle / "Contents/Info.plist", bundle / "Resources/Info.plist", bundle / "Info.plist"]
        info = next((p for p in options if p.is_file()), None)
        require(info, f"Missing required executable bundle Info.plist: {bundle.relative_to(app)}")
        info = physical_path(info, app)
        require(info.stat().st_size <= 4 * 1024**2, "Oversize executable bundle plist")
        with info.open("rb") as f:
            data = plistlib.load(f)
        require(isinstance(data, dict), "Invalid executable bundle plist dictionary")
        exe = plist_executable(info, data, app)
        bundle_exes[bundle] = exe
        expected = "DYLIB" if bundle.suffix == ".framework" else "BUNDLE" if bundle.suffix == ".plugin" else "EXECUTE"
        expected_types[exe] = expected
        fixed = STOCK_BUNDLES.get(str(bundle.relative_to(app)))
        if fixed:
            require(data["CFBundleExecutable"] == fixed[0], f"Unexpected source-defined bundle executable: {bundle.relative_to(app)}")
        if bundle.suffix == ".plugin":
            require(str(bundle.relative_to(app)) == "Contents/PlugIns/CmuxDockTilePlugin.plugin", "Unproven plugin loader host")
            anchors[bundle] = DOCK_HOST
        elif bundle.suffix != ".framework":
            anchors[bundle] = exe
    total = 0
    for p in paths:
        if p.is_symlink():
            require(inside(p, app) and p.exists(), f"External/dangling bundle symlink: {p.relative_to(app)}")
        if not p.is_file() or p.is_symlink():
            continue
        total += p.stat().st_size
        require(total <= 2 * 1024**3, "Bundle exceeds 2 GiB inspection/packaging bound")
        with p.open("rb") as f:
            magic = f.read(4)
        if magic in MACH_MAGIC:
            images[p] = {"path": str(p.relative_to(app)), "sha256": sha256(p)}
        if p.name == "Info.plist":
            require(p.stat().st_size <= 4 * 1024**2, "Oversize bundle plist")
            with p.open("rb") as f:
                data = plistlib.load(f)
            require(isinstance(data, dict), f"Invalid plist dictionary: {p.relative_to(app)}")
            by_arch = data.get("LSMinimumSystemVersionByArchitecture", {})
            require(isinstance(by_arch, dict), "Invalid per-architecture plist minimum OS dictionary")
            # LaunchServices uses the matching architecture entry, with the generic
            # value as fallback. Other architectures never determine this result.
            effective = by_arch.get("x86_64", data.get("LSMinimumSystemVersion"))
            if effective is not None:
                require(version(effective) <= version(TARGET_HOST), f"{p.relative_to(app)} x86_64 plist minimum OS exceeds target host")
            if "MinimumOSVersion" in data:
                require(version(data["MinimumOSVersion"]) <= version(TARGET_HOST), f"{p.relative_to(app)} MinimumOSVersion exceeds target host")
            item = {"path": str(p.relative_to(app)), "sha256": sha256(p),
                    "minimum_os": data.get("LSMinimumSystemVersion", data.get("MinimumOSVersion")),
                    "effective_minimum_os_x86_64": effective if effective is not None else data.get("MinimumOSVersion")}
            if "CFBundleExecutable" in data:
                item["executable"] = str(plist_executable(p, data, app).relative_to(app))
            plists.append(item)
    root_plist = app / "Contents/Info.plist"
    with root_plist.open("rb") as f:
        root = plistlib.load(f)
    require("LSMinimumSystemVersion" in root, "Missing main Info.plist minimum OS")
    platforms = root.get("CFBundleSupportedPlatforms", ["MacOSX"])
    require(isinstance(platforms, list) and "MacOSX" in platforms, "Main Info.plist does not support MacOSX")
    require(root.get("CFBundlePackageType", "APPL") == "APPL", "Main bundle is not an application")
    main = plist_executable(root_plist, root, app)
    require(main in images, "Missing/non-Mach-O main executable")
    require(root.get("NSDockTilePlugIn") == "CmuxDockTilePlugin.plugin", "Missing required stock Dock plugin configuration")
    tool_bytes = 0

    def promote(image):
        nonlocal tool_bytes
        record = images[image]
        if "file_type" not in record:
            report["inspecting_image"] = record["path"]
            # Lossless SELF-rename events cover ALL ancestors through filesystem
            # root, not merely the immediate parent or the leaf's unchanged ctime.
            # Unrelated /tmp and HOME child writes do not fire this fence.
            with directory_fence(image):
                arch = execute("lipo", "-archs", str(image))
                require(len(arch) <= 4096, "Oversize architecture output")
                archs = architectures(arch)  # Check BEFORE requesting the required x86 slice.
                commands = execute("otool", "-arch", "x86_64", "-l", str(image))
                header = execute("otool", "-arch", "x86_64", "-hv", str(image))
            tool_bytes += len(commands.encode()) + len(header.encode())
            require(len(commands) <= 4 * 1024**2 and len(header) <= 65536 and tool_bytes <= 64 * 1024**2,
                    "Mach-O tool output bound exceeded")
            record.update(file_type=file_type(header), architectures=archs, **load_commands(commands),
                          otool_header_x86_64=header.replace(str(app), "$APP"),
                          otool_x86_64=commands.replace(str(app), "$APP"), lipo=arch.replace(str(app), "$APP"))
            if image in remote_paths:
                record["classification"] = "host-required via actual dependency; remote DATA exemption revoked"
        return record

    require(len(images) <= 2048, "Mach-O image count bound exceeded")
    for image in images:
        if image not in remote_paths:
            promote(image)
    for exe, kind in expected_types.items():
        require(exe in images, f"Required embedded executable is not Mach-O: {exe.relative_to(app)}")
        require(promote(exe)["file_type"] == kind, f"Executable bundle lacks an MH_{kind} image: {exe.relative_to(app)}")
    for item in plists:
        if "executable" in item:
            require(app / item["executable"] in images, f"Required embedded executable is not Mach-O: {item['path']}")
    products = dict(STOCK_PRODUCTS)
    optional = app / "Contents/Resources/bin/cmuxd"
    # No producer at this pin, so absence is correct. Presence never bypasses checks.
    if os.path.lexists(optional):
        products["Contents/Resources/bin/cmuxd"] = "EXECUTE"
    report["conditional_products"] = {"cmuxd": "present; validated" if os.path.lexists(optional) else "absent; no source tree/gitlink at pin"}
    for relative, kind in products.items():
        path = physical_path(app / relative, app)
        require(path in images, f"Missing required source-defined helper: {relative}")
        if kind == "EXECUTE":
            require(path.stat().st_mode & 0o111, f"Required helper has no execute bit: {relative}")
        require(promote(path)["file_type"] == kind, f"Required helper is not MH_{kind}: {relative}")

    def display(path):
        return str(path.relative_to(app)) if path.is_relative_to(app) else str(path)

    contexts, seen_images, edge_count = set(), set(), 0

    def walk(image, executable, inherited=(), active=(), depth=0):
        nonlocal edge_count
        require(depth <= MAX_DYLD_DEPTH, "dyld graph depth bound exceeded")
        record = promote(image)
        own = []
        for value in record["rpaths"]:
            require(len(value) <= 4096 and "\x00" not in value, "Invalid rpath")
            path = Path(value) if value == "/usr/lib/swift" else expand_bundle_path(value, image.parent, executable.parent, app)
            if path not in own:
                own.append(path)
        search = tuple(dict.fromkeys([*own, *inherited]))
        context = (image, executable, search)
        if context in contexts:
            return
        require(len(contexts) < MAX_DYLD_CONTEXTS, "dyld context bound exceeded")
        contexts.add(context)
        seen_images.add(image)
        links = check_links(record, image, executable, app, images, search, sdk, promote)
        edge_count += len(links)
        require(edge_count <= MAX_DYLD_EDGES, "dyld edge bound exceeded")
        record.setdefault("loader_executable", display(executable))
        record.setdefault("resolved_dependencies", links)
        record.setdefault("load_contexts", []).append({"loader_executable": display(executable),
            "rpath_stack": [display(path) for path in search], "resolved_dependencies": links})
        for link in links:
            if "image" in link:
                target = app / link["image"]
                if target not in (*active, image):
                    walk(target, executable, search, (*active, image), depth + 1)

    # Every native execution root is independent, including flat helpers and XPCs.
    for image, record in images.items():
        if image not in remote_paths and record["file_type"] == "EXECUTE":
            walk(image, image)
    # Dock loads MH_BUNDLE under its own known executable, never cmux's rpaths.
    for bundle, executable in anchors.items():
        if executable == DOCK_HOST:
            walk(bundle_exes[bundle], DOCK_HOST)
    # Required dlopen products and unreferenced native dylibs still need closure.
    # Already-reached images retain ALL real intermediate-loader contexts, not a
    # second fictional image+main context which would lose the dyld stack.
    for image in images:
        if image in remote_paths or image in seen_images:
            continue
        bundle = max((b for b in anchors if image.is_relative_to(b)), key=lambda b: len(b.parts))
        executable = anchors[bundle]
        root_image = bundle_exes[bundle]
        root_record = promote(root_image)
        inherited = tuple(Path(v) if v == "/usr/lib/swift" else expand_bundle_path(v, root_image.parent, executable.parent, app)
                          for v in root_record["rpaths"])
        walk(image, executable, inherited)
    promoted = [item for item in remote if app / item["path"] in images and "file_type" in images[app / item["path"]]]
    report["remote_payload_data"] = [item for item in remote if item not in promoted]
    report["remote_payload_promoted"] = [{**item, "classification": "host-required via actual dependency"} for item in promoted]
    report.update(dyld_contexts=len(contexts), dyld_edges=edge_count,
                  static_closure_schema=1, provider_product_completeness="VERIFIED locked-provider selected distribution/configuration",
                  provider_proof=provider_proof)
    report.pop("inspecting_image", None)
    report.update(target_host=TARGET_HOST, bundle_bytes=total, plists=plists,
                  bundle_identifier=root.get("CFBundleIdentifier"), bundle_version=root.get("CFBundleVersion"),
                  supported_platforms=root.get("CFBundleSupportedPlatforms"),
                  main_executable=str(main.relative_to(app)), binaries=[r for r in images.values() if "file_type" in r])
    require(images, "No Mach-O images found")
    require(provider.verify(app, providers) == provider_proof, "Provider/source inputs changed during inspection")
    require(bundle_snapshot(app) == before, "Bundle changed during inspection")
    report.update(bundle_identity=before["identity"], inventory=before["inventory"], inventory_schema=1)


def source_contract(source, output, report):
    """Retain the exact source controlling materialization; never trust the index."""
    from native_cmux_providers import _InputBindings
    with _InputBindings() as bindings:
        inventory = bindings.source(source)
        _source_contract(source, output, report, inventory)


def _source_contract(source, output, report, inventory):
    head, tree = inventory["commit"], inventory["tree"]
    require(not os.path.lexists(source / "cmuxd"), "Unexpected cmuxd producer absent from the pinned source tree")
    modules = inventory["submodules"]
    hashes = {}
    for relative, expected in SOURCE_FILES.items():
        path = source / relative
        require(not path.is_symlink() and path.is_file() and sha256(path) == expected,
                f"Pinned source proof mismatch: {relative}")
        # Flat proof names survive explicit diagnostic staging/size accounting
        # and avoid hidden-file upload exclusions.
        dest = output / ("source-proof--" + relative.replace("/", "--"))
        if os.path.lexists(dest):
            require(not dest.is_symlink() and dest.is_file() and sha256(dest) == expected, "Source proof changed")
        else:
            shutil.copyfile(path, dest)
        hashes[relative] = expected
    report.update(source_head=head, source_tree=tree, source_file_sha256=hashes,
                  submodules=modules, source_inventory=inventory)


def recipe_paths(tag):
    require(tag_ok(tag), "Tag must already equal the pinned stock slug")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    temp = Path(os.environ.get("RUNNER_TEMP", ""))
    require(re.fullmatch(r"[1-9][0-9]*", run_id), "Invalid run identity")
    require(temp.is_absolute() and temp.is_dir() and temp.resolve() == temp and
            not any(c in str(temp) for c in "\r\n"), "Noncanonical runner temp")
    root = temp / f"cmux-{tag}-{run_id}"
    derived = root / "derived"
    app = derived / "Build/Products/Debug" / f"cmux DEV {tag}.app"
    return root, derived, app


def ci_identity():
    """Enforce CI-only setup independently of any on-disk preparation receipt."""
    require(sys.platform == "darwin" and run("uname", "-m") == "x86_64", "Requires Darwin/x86_64")
    require(os.environ.get("GITHUB_ACTIONS") == "true" and
            os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "Fresh GitHub-hosted CI runner only")
    require(os.environ.get("GITHUB_REPOSITORY") == CI_REPO and
            os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "Wrong repo or rerun")
    sha = os.environ.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha) and sha == os.environ.get("EXPECTED_WORKFLOW_SHA") and
            run("git", "rev-parse", "HEAD", cwd=Path(__file__).resolve().parent.parent) == sha,
            "Unreviewed workflow checkout")
    for key in os.environ:
        require(key not in ("BASH_ENV", "ENV", "LAUNCH", "DYLD_INSERT_LIBRARIES") and
                not key.startswith(("CMUX_", "ZIG_", "BUN_", "DYLD_", "LD_", "BASH_FUNC_")),
                f"Unexpected recipe override {key}")
    require(run("xcodebuild", "-version") == "Xcode 26.6\nBuild version 17F113", "Selected Xcode differs from reviewed 26.6/17F113")


def check_native_absence():
    # Broad absence is intentional: normal stock mode can stop a matching tag,
    # clean stale state and publish pointers. Never run this on a developer machine.
    candidates = list(Path("/tmp").glob("*cmux*")) + list(Path(tempfile.gettempdir()).glob("*cmux*"))
    home = Path.home()
    candidates += [home / p for p in ("Library/Application Support/cmux", ".config/cmux", ".cmux",
                                      ".local/bin/cmux", ".local/bin/cmux-dev", ".secrets/cmuxterm-dev.env", ".secrets/cmux.env")]
    candidates += list((home / "Library/Developer/Xcode/DerivedData").glob("cmux*"))
    candidates += list(Path("/Applications").glob("*cmux*")) + list((home / "Applications").glob("*cmux*"))
    require(not any(os.path.lexists(p) for p in candidates), "Existing cmux app/socket/tag/discovery state")
    processes = run("ps", "-axo", "comm=").splitlines()
    require(not any(re.search(r"(?:^|/)cmux(?:$|[ .-]|d(?:$|[ /.-]))", p, re.I) for p in processes),
            "Existing cmux app/daemon process")
    require("cmux" not in run("/bin/launchctl", "list").lower(), "Existing cmux launchd state")


def prepare_recipe(source, tag, output, report):
    """CI-only: reject all pre-existing native state BEFORE tools or the sole build."""
    ci_identity()
    root, derived, app = recipe_paths(tag)
    require(not os.path.lexists(root), "Per-run path collision")
    check_native_absence()
    source_contract(source, output, report)
    root.mkdir(mode=0o700)
    report.update(tag=tag, tag_slug=tag, app_name=f"cmux DEV {tag}", app=str(app),
                  derived_data=str(derived), recipe_root=str(root), backend="local",
                  recipe="stock tagged reload; no build-only; stock CI app/helper startup permitted by D019; no global CLI links")
    with Path(os.environ["GITHUB_ENV"]).open("a") as env:
        env.write(f"NATIVE_CI_ROOT={root}\nDERIVED_DATA={derived}\nNATIVE_APP_PATH={app}\n")


def materialized_recipe(source, tag, output, report):
    root, derived, app = recipe_paths(tag)
    prepared = json.loads((output / "prepare.json").read_text())
    require(prepared.get("ok") is True and prepared.get("tag_slug") == tag and
            prepared.get("recipe_root") == str(root) and prepared.get("app") == str(app) and
            prepared.get("derived_data") == str(derived), "Recipe path/identity changed")
    require(root.resolve() == root and app.resolve() == app and app.is_dir(), "Missing/redirected final tagged app")
    require(not list(app.parent.glob(f".cmux DEV {tag}.reload-*.app")), "Unpublished staging bundle remains")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    require(info.get("CFBundleName") == f"cmux DEV {tag}" and
            info.get("CFBundleIdentifier") == "com.cmuxterm.app.debug." + tag.replace("-", "."),
            "Tagged app identity mismatch")
    environment = info.get("LSEnvironment", {})
    require(isinstance(environment, dict), "Missing tagged app environment")
    port = str(environment.get("CMUX_PORT", ""))
    require(re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535, "Missing local backend port")
    origin = f"http://localhost:{port}"
    require(environment.get("CMUX_TAG") == tag and
            environment.get("CMUX_BUNDLED_CLI_PATH") == str(app / "Contents/Resources/bin/cmux") and
            environment.get("CMUX_SOCKET_PATH") == f"/tmp/cmux-debug-{tag}.sock" and
            environment.get("CMUX_API_BASE_URL") == origin and environment.get("CMUX_VM_API_BASE_URL") == origin and
            not environment.get("CMUX_DEV_BACKEND_URL"), "App is not the expected tagged/local backend artifact")
    source_contract(source, output, report)
    report.update(app=str(app), tag_slug=tag, app_name=info["CFBundleName"], backend="local", backend_origin=origin,
                  complete_bundle_validation="pending inspect/package full-inventory and helper checks")


def setup_tools(source, tag, output, report):
    """Only called by the future fresh CI job; never setup.sh or a real build retry."""
    ci_identity()
    root, _, _ = recipe_paths(tag)
    prepared = json.loads((output / "prepare.json").read_text())
    require(prepared.get("ok") is True and prepared.get("source_head") == SOURCE_SHA and
            prepared.get("source_tree") == SOURCE_TREE and prepared.get("tag_slug") == tag and
            prepared.get("recipe_root") == str(root) and
            root.resolve() == root and root.is_dir(), "Missing fresh-CI preparation")
    source_contract(source, output, report)
    require(run("xcodebuild", "-version") == "Xcode 26.6\nBuild version 17F113", "Selected Xcode changed")
    tools = root / "tools"
    require(not os.path.lexists(tools), "Tool setup cannot repeat")
    tools.mkdir(mode=0o700)
    work = tools / "work"
    work.mkdir()
    required = run("bash", "scripts/ghostty-zig-version.sh", str(source), cwd=source)
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", required), "Invalid pinned Ghostty Zig requirement")
    # The source installer verifies the release index SHA-256, plus minisign if
    # available. Mirrors/retries share ONE 360s download budget, not build attempts.
    env = dict(os.environ, RUNNER_TEMP=str(work), ZIG_FORCE_LOCAL_INSTALL="1",
               ZIG_INSTALL_ROOT=str(tools / "zig"), ZIG_REQUIRED=required,
               ZIG_DOWNLOAD_ATTEMPTS="2", ZIG_DOWNLOAD_BUDGET_SECONDS="360")
    for key in ("GITHUB_ENV", "GITHUB_PATH"):
        env.pop(key, None)  # Do not let the source installer export CMUX_ZIG globally.
    report.update(zig_required=required, zig_download_budget_seconds=360, bun_version=BUN_VERSION,
                  setup_scope="dependencies only; no native build invoked", download_retries_are_not_build_retries=True)
    zig_log = run("bash", "scripts/install-zig-ci.sh", cwd=source, env=env, timeout=420).encode()
    report["zig_setup_stdout"] = retain_log(zig_log, output, "zig-setup.log", len(zig_log),
                                             "pinned force-local Zig installer stdout; stderr not retained")
    zig = tools / "zig" / f"zig-x86_64-macos-{required}" / "zig"
    require(zig.is_file() and zig.resolve().is_relative_to(tools), "Zig escaped isolated tools")
    require(run(str(zig), "version") == required, "Installed Zig must EXACTLY match pinned manifest")
    report["zig_sha256"] = sha256(zig)
    install_bun(tools, report)
    try:
        run("xcrun", "--sdk", "macosx", "metal", "--version")
        report["metal_setup"] = "already working; no component download"
    except ValueError:
        # Apple's selected-Xcode component mechanism; no sudo/login/OS grant.
        run("xcodebuild", "-downloadComponent", "MetalToolchain", timeout=600)
        run("xcrun", "--sdk", "macosx", "metal", "--version")
        report["metal_setup"] = "one official selected-Xcode component download"
    with Path(os.environ["GITHUB_PATH"]).open("a") as path:
        path.write(f"{zig.parent}\n{tools / 'bun'}\n")
    report["readiness"] = "setup complete; native preflight and final binary compatibility still required"


def install_bun(tools, report):
    asset = "bun-darwin-x64.zip"
    base = f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}"
    for filename in ("SHASUMS256.txt", asset):
        run("curl", "-q", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--tlsv1.2", "--retry", "2", "--connect-timeout", "10", "--max-time", "120",
            "--max-filesize", str(100 * 1024 * 1024 if filename == asset else 256 * 1024),
            "--output", str(tools / filename), f"{base}/{filename}", timeout=400)
    archive, checksums = tools / asset, tools / "SHASUMS256.txt"
    require(checksums.stat().st_size <= 256 * 1024 and archive.stat().st_size <= 100 * 1024 * 1024,
            "Bun download size cap")
    matches = re.findall(r"(?m)^([a-fA-F0-9]{64})\s+\*?bun-darwin-x64\.zip\s*$", checksums.read_text())
    require(len(matches) == 1 and sha256(archive) == matches[0].lower(), "Official Bun checksum mismatch")
    dest = tools / "bun"
    dest.mkdir()
    with zipfile.ZipFile(archive) as zf:
        entries = [i for i in zf.infolist() if i.filename == "bun-darwin-x64/bun"]
        require(len(entries) == 1 and entries[0].file_size <= 150 * 1024 * 1024 and
                stat.S_ISREG(entries[0].external_attr >> 16), "Invalid Bun archive binary")
        with zf.open(entries[0]) as src, (dest / "bun").open("xb") as target:
            shutil.copyfileobj(src, target)
    (dest / "bun").chmod(0o755)
    require(run(str(dest / "bun"), "--version") == BUN_VERSION, "Bun version mismatch")
    report.update(bun_url=f"{base}/{asset}", bun_archive_sha256=sha256(archive),
                  bun_verification="official release SHA-256 manifest; same-origin, not independent signature")
    archive.unlink()  # Per-run downloaded tool files are not uploaded as build evidence.


def preflight(source, tag, report):
    require(sys.platform == "darwin", "Requires Darwin/x86_64; no label fallback")
    from native_cmux_providers import _InputBindings
    with _InputBindings() as bindings:
        report["source_inventory"] = bindings.source(source)
        _preflight(source, tag, report)


def _preflight(source, tag, report):
    report.update(runner_platform=sys.platform, runner_arch=run("uname", "-m"))
    require(sys.platform == "darwin" and report["runner_arch"] == "x86_64", "Requires Darwin/x86_64; no label fallback")
    require(tag_ok(tag), "Use a new exclusive d014- tag (10-60 slug characters)")
    require(os.environ.get("GITHUB_REPOSITORY") == CI_REPO, "Wrong workflow repository")
    require(os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "Reruns are forbidden; this is a one-build route")
    workflow_sha = os.environ.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", workflow_sha) and workflow_sha == os.environ.get("EXPECTED_WORKFLOW_SHA"), "Workflow SHA mismatch")
    report.update(workflow_sha=workflow_sha, workflow_ref=os.environ.get("GITHUB_WORKFLOW_REF"),
                  run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=1, tag=tag,
                  runner_os=run("sw_vers", "-productVersion"), runner_arch=run("uname", "-m"),
                  runner_image={k: os.environ.get(k) for k in ("ImageOS", "ImageVersion")})
    require(run("git", "rev-parse", "HEAD", cwd=Path(__file__).resolve().parent.parent) == workflow_sha,
            "Harness checkout does not match workflow SHA")
    head = report["source_inventory"]["commit"]
    report.update(source_repo=SOURCE_REPO, source_ref=SOURCE_REF, source_head=head,
                  source_tree=report["source_inventory"]["tree"],
                  submodules=report["source_inventory"]["submodules"])
    require((source / ".xcode-version").read_text().strip() == "26", "Unexpected source Xcode requirement")
    xcode = run("xcodebuild", "-version")
    report["xcode"] = xcode
    require(xcode == "Xcode 26.6\nBuild version 17F113", "Selected Xcode 26.6/17F113 required")
    sdk_version = run("xcrun", "--sdk", "macosx", "--show-sdk-version")
    report["sdk_version"] = sdk_version
    require(version(sdk_version) >= version("26"), "macOS 26 SDK required")
    run("xcrun", "--sdk", "macosx", "--find", "metal")
    metal = run("xcrun", "--sdk", "macosx", "metal", "--version")
    report["metal"] = metal
    require(run("bun", "--version") == BUN_VERSION, "Pinned Bun version required")
    for tool in ("git", "python3", "swift", "clang", "lipo", "otool", "codesign", "ditto", "curl", "shasum", "rsync", "unzip", "perl", "zig", "bun", "cargo", "rustc"):
        require(shutil.which(tool), f"Missing expected prerequisite {tool}; no global installation/fallback permitted")
    for key in ("MACOSX_DEPLOYMENT_TARGET", "ARCHS", "SDKROOT", "XCODE_XCCONFIG_FILE", "SWIFT_EXEC", "RUSTUP_TOOLCHAIN", "RUSTC"):
        require(key not in os.environ, f"Unexpected toolchain override {key}")
    require(not any(k.startswith("CMUX_") for k in os.environ), "Unexpected CMUX environment override")
    zig = run("zig", "version")
    # Read the exact version from the pinned recursive source, not a moving release.
    # This helper checks the installed tool; it does not install or compile anything.
    zig_required = run("bash", "scripts/ghostty-zig-version.sh", str(source), cwd=source)
    require(zig == zig_required, "Zig must exactly match the pinned Ghostty manifest")
    run("bash", "-c", 'source scripts/ghostty-zig-version.sh; ghostty_require_compatible_zig "$PWD"', cwd=source)
    report.update(zig=zig, zig_source_requirement=zig_required, cargo=run("cargo", "--version"),
                  rustc=run("rustc", "-vV"), metal=metal)
    require("host: x86_64-apple-darwin" in report["rustc"], "Rust host is not x86_64-apple-darwin")
    project = (source / "cmux.xcodeproj/project.pbxproj").read_text()
    targets = sorted(set(re.findall(r"MACOSX_DEPLOYMENT_TARGET = ([^;]+);", project)))
    require(targets and all(version(t.strip('"')) <= version(TARGET_HOST) for t in targets), "Source project has missing/incompatible deployment target")
    archs = sorted(set(re.findall(r"(?m)^\s*ARCHS = ([^;]+);", project)))
    require(all("x86_64" in a or "$(ARCHS_STANDARD)" in a or "$(ARCHS_STANDARD_64_BIT)" in a for a in archs),
            "Source project declares an unsupported/arm-only architecture route")
    report["project_architecture_declarations"] = archs or ["Xcode standard host architecture (no override)"]
    require(version(run("sw_vers", "-productVersion")) >= version("14"), "Source requires macOS 14+")
    check_native_absence()  # Recheck after dependency setup, immediately before the sole stock call.
    # Source-owned build phases/reload resolve dependencies. Never call setup.sh (global installs).
    report.update(source_repo=SOURCE_REPO, source_ref=SOURCE_REF, source_head=head,
                  source_tree=report["source_inventory"]["tree"],
                  workflow_sha=workflow_sha, workflow_ref=os.environ.get("GITHUB_WORKFLOW_REF"),
                  run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=1, tag=tag,
                  runner_os=run("sw_vers", "-productVersion"), runner_arch=run("uname", "-m"),
                  runner_image={k: os.environ.get(k) for k in ("ImageOS", "ImageVersion")},
                  xcode=xcode, sdk_version=sdk_version, metal=metal, swift=run("swift", "--version"),
                  zig=zig, zig_source_requirement=zig_required, bun=run("bun", "--version"),
                  workflow_file_sha256=sha256(Path(__file__).resolve().parent.parent / WORKFLOW),
                  deployment_target_declarations=targets,
                  submodules=report["source_inventory"]["submodules"], target_host=TARGET_HOST)


LOG_LIMIT = 2 * 1024 * 1024  # Each retained stdout/native log, not source-owned originals.


def log_provenance():
    return {"expected_source_sha": SOURCE_SHA, "source_repo": SOURCE_REPO,
            "workflow_sha": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
            "tag": os.environ.get("BUILD_TAG"), "artifact_retention_days": 1}


def retain_log(data, output, name, original_bytes, origin):
    # No environment dumps, credential-bearing git config or raw exception stderr.
    # Build steps receive no Actions token. Defence in depth for recognizable tokens;
    # this is not a promise to detect arbitrary secrets printed by third-party tools.
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", text)
    text = re.sub(r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)\s+\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([?&](?:token|access_token|key|password|secret)=)[^\s&#]+", r"\1[REDACTED]", text)
    encoded = text.encode()
    retained = encoded[-LOG_LIMIT:]
    path = output / name
    require(not path.exists() and not path.is_symlink(), "Log output already exists; no overwrite")
    path.write_bytes(retained)
    return {"path": name, "origin": origin, "original_bytes_observed": original_bytes,
            "retained_bytes": len(retained), "truncated": original_bytes > len(data) or len(encoded) > LOG_LIMIT,
            "sha256": sha256(path), "redaction": "known credential patterns only; UTF-8 replacement permitted"}


def capture_build_stdout(stream, output):
    output.mkdir(parents=True, exist_ok=True)
    tail, total = bytearray(), 0
    for chunk in iter(lambda: stream.read(64 * 1024), b""):
        total += len(chunk)
        tail.extend(chunk)
        if len(tail) > LOG_LIMIT:
            del tail[:-LOG_LIMIT]
    meta = retain_log(bytes(tail), output, "build-stdout.log", total, "stock reload stdout+stderr pipe")
    (output / "stdout-capture.json").write_text(json.dumps({**log_provenance(), "stdout": meta}, indent=2) + "\n")


def collect_build_logs(output, tag, temp_root=Path("/tmp")):
    require(tag_ok(tag), "Invalid native-log tag")
    # The permitted lowercase slug is unchanged by stock TAG_SLUG normalization.
    path = temp_root / f"cmux-reload-{tag}.log"
    meta = {**log_provenance(), "tag": tag, "native": {"state": "absent", "origin": f"/tmp/cmux-reload-{tag}.log"}}
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), "Native build log is not a regular file")
            stream.seek(max(0, before.st_size - LOG_LIMIT))
            data = stream.read(LOG_LIMIT)
            after = os.fstat(stream.fileno())
        meta["native"] = retain_log(data, output, "native-build.log", before.st_size,
                                    f"/tmp/cmux-reload-{tag}.log")
        meta["native"].update(state="captured", size_stable_during_capture=before.st_size == after.st_size)
    stdout = output / "stdout-capture.json"
    meta["stdout"] = json.loads(stdout.read_text())["stdout"] if stdout.is_file() else {"state": "absent"}
    meta["limits"] = {"per_retained_log_bytes": LOG_LIMIT, "total_artifact_bytes": MAX_UPLOAD,
                      "source_owned_inner_log_bounded": False,
                      "actions_console_retention_controlled_here": False}
    (output / "build-logs.json").write_text(json.dumps(meta, indent=2) + "\n")


DIAGNOSTIC_FILES = (
    "prepare.json", "tools.json", "materialized.json", "preflight.json", "inspect.json",
    "package.json", "capture.json", "results.json", "providers.json", "stdout-capture.json",
    "providers-collection.json", "zig-setup.log",
    "build-logs.json", "build-stdout.log", "native-build.log",
    # Fixed pinned catalog, never a source-proof glob or arbitrary evidence tree.
    *("source-proof--" + relative.replace("/", "--") for relative in SOURCE_FILES),
)


def _stage_evidence_file(source, stage, name, limit, expected=None):
    """Copy bounded regular bytes to an exclusively created, attempt-private file."""
    destination = stage / name
    created = False
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                    "Evidence input is not a single-link regular file")
            require(before.st_size <= limit, "Evidence input exceeds cap")
            digest, size = hashlib.sha256(), 0
            with destination.open("xb") as retained:
                created = True
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    size += len(chunk)
                    require(size <= limit, "Evidence input exceeds cap")
                    digest.update(chunk)
                    retained.write(chunk)
            after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, k) == getattr(after, k) for k in fields) and size == before.st_size,
                "Evidence input changed during retention")
        row = {"path": name, "bytes": size, "sha256": digest.hexdigest()}
        if expected is not None:
            require(row["bytes"] == expected["bytes"] and row["sha256"] == expected["sha256"],
                    "Archive does not match the successful package proof")
        return row
    except Exception:
        if created:
            destination.unlink()  # Only this attempt-private destination; never the public input.
        raise


def _discard_private_app_stage(stage):
    stage.chmod(0o700)
    shutil.rmtree(stage)


def _validate_sealed_archive(archive, expected):
    """Independently hash the retained file, not just the input copy stream."""
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        os.fchmod(stream.fileno(), 0o400)
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and not (before.st_mode & 0o222), "App staging is not sealed regular bytes")
        digest, size = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            size += len(chunk)
            require(size <= expected["bytes"], "Sealed archive exceeds package proof")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, k) == getattr(after, k) for k in fields)
                and os.path.samestat(archive.lstat(), after)
                and size == expected["bytes"] and digest.hexdigest() == expected["sha256"],
                "Sealed staging differs from the verified package bytes")
    return {"bytes": size, "sha256": digest.hexdigest()}


def seal_app_archive(output, report):
    require(os.environ.get("PACKAGE_OUTCOME") == "success"
            and os.environ.get("DIAGNOSTICS_OUTCOME") == "success",
            "App admission requires successful package and diagnostics steps")
    stage = Path(tempfile.mkdtemp(prefix="app-seal-", dir=output))
    try:
        _stage_evidence_file(output / "upload/package.json", stage, "package-proof.json", 8 * 1024**2)
        proof = json.loads((stage / "package-proof.json").read_text())
        require(isinstance(proof, dict) and proof.get("mode") == "package"
                and proof.get("ok") is True and proof.get("archive_verified") is True
                and proof.get("inventory_schema") == 1
                and proof.get("expected_source_sha") == SOURCE_SHA
                and proof.get("archive") == "cmux-build-only.zip"
                and type(proof.get("bytes")) is int and proof["bytes"] >= 0
                and re.fullmatch(r"[0-9a-f]{64}", proof.get("sha256", "")) is not None,
                "No successful verified package receipt")
        used = 0
        for name in (*DIAGNOSTIC_FILES, "artifact-files.json", "diagnostics.json"):
            try:
                observed = (output / "upload" / name).lstat()
            except FileNotFoundError:
                continue
            require(stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1,
                    "Diagnostic staging is not regular bytes")
            used += observed.st_size
        archive = stage / "cmux-build-only.zip"
        _stage_evidence_file(output / archive.name, stage, archive.name,
                             MAX_UPLOAD - used - 64 * 1024, proof)
        stage.chmod(0o500)
        retained = _validate_sealed_archive(archive, proof)
        path = str(archive)
        require(not any(c in path for c in "\r\n*?[]"), "Unsafe app upload path")
        report.update(verified_seal=True, archive_path=path, **retained)
    except Exception:
        report["verified_seal"] = False
        try:
            _discard_private_app_stage(stage)
        except Exception:
            report["private_cleanup_failed"] = True
        raise  # Rejection stays a rejection even if private cleanup fails.


def finalize_evidence(output):
    # Exclusive private staging, not a recursive upload of the public evidence tree.
    # A prior directory is never reused, overwritten or destructively cleaned.
    stage = output / "upload"
    stage.mkdir(mode=0o700)
    files = []
    proof_hashes = {"source-proof--" + relative.replace("/", "--"): digest
                    for relative, digest in SOURCE_FILES.items()}
    for name in DIAGNOSTIC_FILES:
        try:
            row = _stage_evidence_file(output / name, stage, name,
                                      8 * 1024**2 if name.endswith(".json") or name in proof_hashes else LOG_LIMIT)
        except FileNotFoundError:
            continue
        if name in proof_hashes:
            require(row["sha256"] == proof_hashes[name] and sha256(stage / name) == proof_hashes[name],
                    "Retained pinned source proof mismatch: " + name)
        files.append(row)
    used = sum(row["bytes"] for row in files)
    require(used + 64 * 1024 <= MAX_UPLOAD, "Final evidence exceeds total upload cap")
    archive_state = "never admitted by diagnostics"
    manifest = {**log_provenance(), "files": files, "archive": archive_state,
                "excludes": ["artifact-files.json", "diagnostics.json", "all non-allowlisted public entries"]}
    content = json.dumps(manifest, indent=2) + "\n"
    require(len(content.encode()) <= 32 * 1024, "Final manifest exceeds reserve")
    (stage / "artifact-files.json").write_text(content)
    return {"directory": "upload", "archive": archive_state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "tools", "materialized", "preflight", "inspect", "package", "seal", "capture", "diagnostics"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--providers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"mode": args.mode, "ok": False, "source_repo": SOURCE_REPO, "expected_source_sha": SOURCE_SHA,
              "workflow_sha": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
              "tag": args.tag or os.environ.get("BUILD_TAG")}
    try:
        require(args.source is not None and args.tag is not None if args.mode in ("prepare", "tools", "materialized", "preflight")
                else args.app is not None if args.mode in ("inspect", "package") else True,
                "recipe/preflight requires --source/--tag; inspect/package require --app")
        if args.mode in ("prepare", "tools", "materialized"):
            {"prepare": prepare_recipe, "tools": setup_tools, "materialized": materialized_recipe}[args.mode](
                args.source.resolve(), args.tag, args.output, report)
        elif args.mode == "preflight":
            preflight(args.source.resolve(), args.tag, report)
        elif args.mode == "inspect":
            require(sys.platform == "darwin", "Real inspection requires Apple lipo/otool on Darwin")
            inspect_app(args.app, report, providers=args.providers)
        elif args.mode == "package":
            package_app(args.app, args.output, report)
        elif args.mode == "seal":
            seal_app_archive(args.output, report)
        elif args.mode == "capture":
            capture_build_stdout(sys.stdin.buffer, args.output)
        else:
            collect_build_logs(args.output, args.tag or os.environ.get("BUILD_TAG"))
            report["upload"] = finalize_evidence(args.output)
        report["ok"] = True
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError,
            zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
        # Do not expose subprocess stderr (which may contain dependency service responses).
        report["error"] = str(exc) if not isinstance(exc, subprocess.SubprocessError) else "Tool failed; inspect the named Actions step; no fallback attempted"
    content = json.dumps(report, indent=2) + "\n"
    if len(content.encode()) > 8 * 1024 * 1024:
        report = {"mode": args.mode, "ok": False, "error": "Diagnostic report exceeds 8 MiB cap; no acceptance claim"}
        content = json.dumps(report, indent=2) + "\n"
    (args.output / f"{args.mode}.json").write_text(content)
    if args.mode == "diagnostics" and report["ok"]:
        (args.output / "upload" / "diagnostics.json").write_text(content)
    if args.mode == "seal":
        # Keep every diagnostic artifact path under the same flat upload root.
        # A receipt-staging failure also blocks all positive app outputs below.
        _stage_evidence_file(args.output / "seal.json", args.output / "upload", "seal.json", 64 * 1024)
    if args.mode == "seal" and report["ok"] and report.get("verified_seal") is True:
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
                outputs.write(f"archive_path={report['archive_path']}\nverified=true\n")
    print(f"{args.mode}: {'PASS' if report['ok'] else 'FAIL'}" + (f": {report['error']}" if 'error' in report else ""))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
