"""CI-owned core-runtime contract. Python standard library only; never installs anything."""
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA = "gsd-owned-runtime-v1"
MAX_BYTES = 4 * 1024**3
MAX_ARCHIVE = 2 * 1024**3
MAX_MEMBERS = 250000
MAX_METADATA = 64 * 1024**2
ADDON = "runtime/native/addon/gsd_engine.darwin-x64.node"
EXCLUDED = {".git", ".cache", ".next", ".turbo", ".npm", ".pnpm-store", ".bin",
            ".ssh", ".aws", ".gnupg", ".gsd", ".credentials", ".DS_Store",
            ".npmrc", ".netrc", ".pypirc", ".modules.yaml", ".pnpm-workspace-state-v1.json"}
PAYLOADS = ("runtime.tar.gz", "file-manifest.json", "provenance.json")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def load_json(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_METADATA,
            "missing, linked or oversized metadata")
    return json.loads(path.read_bytes())


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "expected full lowercase 40-hex SHA")
    return value


def safe_path(value):
    require(isinstance(value, str) and 0 < len(value.encode()) <= 4096, "invalid path length")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value) and "\\" not in value and ":" not in value,
            "unsafe path characters")
    parts = value.split("/")
    require(all(p not in ("", ".", "..") for p in parts) and parts[0] == "runtime", "unsafe archive path")
    require(not any(p in EXCLUDED or p == ".env" or p.startswith(".env.") for p in parts), "excluded archive path")
    return value


def link_path(name, target):
    require(isinstance(target, str) and 0 < len(target.encode()) <= 4096 and not target.startswith("/"),
            "absolute/empty symlink")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in target) and "\\" not in target and ":" not in target,
            "unsafe symlink target")
    return safe_path(posixpath.normpath(posixpath.join(posixpath.dirname(name), target)))


def validate_entries(entries):
    require(0 < len(entries) <= MAX_MEMBERS, "member count limit")
    names = {}
    folded = set()
    total = 0
    for e in entries:
        name = safe_path(e["path"])
        key = unicodedata.normalize("NFD", name).casefold()
        require(name not in names and key not in folded, "duplicate/case/Unicode-colliding member")
        names[name] = e
        folded.add(key)
        require(e["type"] in ("file", "directory", "symlink"), "unsupported member type")
        require(type(e["mode"]) is int and 0 <= e["mode"] <= 0o777, "unsafe mode")
        require(type(e["size"]) is int and 0 <= e["size"] <= MAX_BYTES, "invalid member size")
        if e["type"] != "file":
            require(e["size"] == 0, "non-file data")
        else:
            require(re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), "invalid file digest")
        total += e["size"]
        require(total <= MAX_BYTES, "unpacked byte limit")
    require(names.get("runtime", {}).get("type") == "directory", "missing runtime root")
    for name, e in names.items():
        parent = posixpath.dirname(name)
        while parent:
            require(names.get(parent, {}).get("type") == "directory", "missing or non-directory parent")
            parent = posixpath.dirname(parent)
        if e["type"] == "symlink":
            link_path(name, e["target"])
            # Preserve component order: normalizing `link/..` before expanding link
            # can hide an escape on disk even when the lexical path stays inside.
            pending = posixpath.dirname(name).split("/") + e["target"].split("/")
            resolved = []
            hops = 0
            while pending:
                part = pending.pop(0)
                if part in ("", "."):
                    continue
                if part == "..":
                    require(len(resolved) > 1, "symlink escapes runtime")
                    resolved.pop()
                    continue
                resolved.append(part)
                prefix = "/".join(resolved)
                require(prefix in names, "dangling symlink")
                if names[prefix]["type"] == "symlink":
                    hops += 1
                    require(hops <= 80, "symlink cycle/depth")
                    target = names[prefix]["target"]
                    link_path(prefix, target)
                    resolved.pop()
                    pending = target.split("/") + pending
                elif pending:
                    require(names[prefix]["type"] == "directory", "symlink through file")
    return total


def inventory(root):
    entries = []
    for base, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(base)] + [Path(base) / x for x in sorted(dirs + files)]:
            if path != Path(base) and path.is_dir() and not path.is_symlink():
                continue
            info = path.lstat()
            name = "runtime" + ("/" + path.relative_to(root).as_posix() if path != root else "")
            e = {"path": name, "mode": stat.S_IMODE(info.st_mode), "size": 0}
            if stat.S_ISLNK(info.st_mode):
                e.update(type="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                e.update(type="directory")
            else:
                require(stat.S_ISREG(info.st_mode), "special file in runtime")
                e.update(type="file", size=info.st_size, sha256=digest(path))
            entries.append(e)
    entries.sort(key=lambda e: e["path"])
    validate_entries(entries)
    return entries


def command(args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=60).strip()


def workspace_manifests(source):
    return sorted(set(source.glob("packages/*/package.json")) | set(source.glob("extensions/*/package.json")) |
                  ({source / "web/package.json"} if (source / "web/package.json").exists() else set()))


def stage(source, root):
    """Shipping allowlist + full project-local dependency closure (no production prune)."""
    package = load_json(source / "package.json")
    workspaces = workspace_manifests(source)
    internal = {}
    for path in workspaces:
        pkg = load_json(path)
        if path.parent.parent.name == "packages" and (pkg.get("gsd", {}).get("linkable") or pkg.get("main")):
            require(re.fullmatch(r"@(gsd|opengsd)/[a-z0-9][a-z0-9._-]*", pkg["name"]), "unexpected internal package name")
            require(path.resolve().is_relative_to(source), "workspace manifest outside source")
            internal[pkg["name"]] = path.parent.relative_to(source).as_posix()
    for name in ("pi-agent-core", "pi-ai", "pi-tui", "pi-coding-agent"):
        if "@gsd/" + name in internal:
            internal["@earendil-works/" + name] = internal["@gsd/" + name]
    used = [0, 0]

    def copy(path):
        rel = path.relative_to(source)
        if any(p in EXCLUDED or p == ".env" or p.startswith(".env.") for p in rel.parts):
            return
        dest = root / rel
        if dest.exists() or dest.is_symlink():
            return
        used[0] += 1
        require(used[0] <= MAX_MEMBERS, "staging member count limit")
        require(path.resolve().is_relative_to(source), "source link escapes checkout")
        dest.parent.mkdir(parents=True, exist_ok=True)
        internal_name = "/".join(rel.parts[-2:]) if len(rel.parts) >= 3 and rel.parts[-3] == "node_modules" else None
        if internal_name in internal:
            dest.symlink_to(os.path.relpath(root / internal[internal_name], dest.parent))
        elif path.is_symlink():
            dest.symlink_to(os.path.relpath(root / path.resolve().relative_to(source), dest.parent))
        elif path.is_dir():
            dest.mkdir(mode=0o755)
            for child in sorted(path.iterdir()):
                copy(child)
        else:
            require(path.is_file(), "special source file")
            used[1] += path.stat().st_size
            require(used[1] <= MAX_BYTES, "staging byte limit")
            shutil.copyfile(path, dest)
            dest.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)

    root.mkdir(mode=0o755)
    for pattern in package["files"]:
        require(isinstance(pattern, str) and not pattern.startswith(("/", "!")) and ".." not in PurePosixPath(pattern).parts,
                "unsupported shipping allowlist")
        for path in sorted(source.glob(pattern)):
            copy(path)
    for path in [source / "package.json", source / "pnpm-lock.yaml", source / "pnpm-workspace.yaml"] + workspaces:
        copy(path)
    require((source / "node_modules").is_dir() and not (source / "node_modules").is_symlink(), "missing/local dependency tree")
    for base in [source] + [p.parent for p in workspaces]:
        if (base / "node_modules").exists():
            copy(base / "node_modules")
    copy(source / "native/addon/gsd_engine.darwin-x64.node")
    for name, directory in internal.items():
        dest = root / "node_modules" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        require(not dest.exists(), "internal module must not be a registry copy")
        dest.symlink_to(os.path.relpath(root / directory, dest.parent))
        pkg = load_json(root / directory / "package.json")
        main = root / directory / pkg["main"]
        require(main.resolve().is_relative_to(root / directory) and main.is_file(), "missing/escaping compiled workspace entry")
    for entry in ["dist/bootstrap.js", "dist/loader.js", "packages/native/dist/native.js",
                  "packages/mcp-server/bin/gsd-mcp-server.js"]:
        require((root / entry).is_file() and (root / entry).stat().st_size > 0, "missing core runtime entry: " + entry)
    return internal


def source_inputs(source):
    tracked = [name for name in command(["git", "ls-files", "-z"], source).split("\0") if name]
    groups = {"dependencies": [], "native": []}
    for name in tracked:
        if name in ("pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc") or name.endswith("package.json"):
            groups["dependencies"].append(name)
        if name.startswith(("native/", "packages/native/")):
            groups["native"].append(name)
    result = {}
    for key, names in groups.items():
        files = [{"path": name, "bytes": (source / name).stat().st_size, "sha256": digest(source / name)} for name in names]
        result[key] = {"files": files, "bytes": sum(f["bytes"] for f in files),
                       "sha256": hashlib.sha256(json_bytes(files)).hexdigest()}
    return result


def package_runtime(source, output, context):
    source = source.resolve(strict=True)
    require(not output.exists() and not output.is_symlink(), "output must be new")
    require(not output.resolve().is_relative_to(source), "output cannot be inside source")
    sha(context["expected_sha"])
    require(command(["git", "rev-parse", "HEAD"], source) == context["expected_sha"], "source HEAD mismatch")
    require(command(["git", "rev-parse", "--abbrev-ref", "HEAD"], source) == "HEAD", "source must be detached")
    command(["git", "diff", "--quiet", "HEAD", "--"], source)
    libraries = context["build"].get("native_libraries", "").splitlines()[1:]
    library_ids = context["build"].get("native_library_id", "").splitlines()[1:]
    require(len(library_ids) <= 1 and all(line.strip().endswith("libgsd_engine.dylib") for line in library_ids),
            "unexpected native library identity")
    own_id = library_ids[0].strip() if library_ids else None
    require(libraries and all(line.strip().startswith(("/usr/lib/", "/System/Library/")) or
                             (own_id and line.strip().split(" (", 1)[0] == own_id) for line in libraries),
            "native addon links non-system or unrecorded libraries")
    require(context["build"].get("cargo_encoded_rustflags") == "" and context["build"].get("rustflags") == "",
            "portable Rust flags contract mismatch")
    # Only tracked build inputs are fingerprinted. Record install/native downloads as non-reproducible inputs below.
    output.mkdir()
    internal = stage(source, output / "runtime")
    entries = inventory(output / "runtime")
    require(next(e for e in entries if e["path"] == ADDON)["type"] == "file", "actual addon required")
    manifest = {"schema": SCHEMA, "entries": entries}
    (output / "file-manifest.json").write_bytes(json_bytes(manifest))
    provenance = {
        "schema": SCHEMA,
        "source": {"repository": "pimmink/gsd-pi", "sha": context["expected_sha"],
                   "tree": command(["git", "rev-parse", "HEAD^{tree}"], source),
                   "requested_ref": context["source_ref"], "observed_branch_head": sha(context["observed_branch_head"]),
                   "observed_at": context["observed_at"]},
        "workflow": {"repository": "pimmink/gsd-pi-ci", "sha": sha(context["workflow_sha"]),
                     "ref": context["workflow_ref"], "run_id": context["run_id"],
                     "attempt": context["attempt"], "job": "owned-runtime"},
        "platform": {"os": "darwin", "arch": "x64", "runner": "macos-15-intel"},
        "build": context["build"], "created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": source_inputs(source), "internal_packages": internal,
        "manifest_sha256": digest(output / "file-manifest.json"),
        "closure": {"kind": "full-installed-core-runtime", "max_bytes": MAX_BYTES, "max_members": MAX_MEMBERS,
                    "bytes": sum(e["size"] for e in entries), "members": len(entries),
                    "native": next(e for e in entries if e["path"] == ADDON),
                    "dependency_bytes": sum(e["size"] for e in entries if "node_modules" in e["path"].split("/")),
                    "dependencies_sha256": hashlib.sha256(json_bytes([e for e in entries if "node_modules" in e["path"].split("/")])).hexdigest()},
        "limits": ["Not npm/release/merge parity; no web build, browser downloads or RTK.",
                   "Node executable and macOS system libraries are host prerequisites, not bundled.",
                   "Node 24.15.0 and pnpm 10.12.1 pinned; Rust stable, runner image and registry lifecycle downloads are not reproducible pins.",
                   "Input fingerprints and installed bytes are evidence, not proof of deterministic rebuilds or fresh host presentation acceptance."]}
    (output / "provenance.json").write_bytes(json_bytes(provenance))
    archive = output / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        for e in entries:
            info = tarfile.TarInfo(e["path"])
            info.mode = e["mode"]
            info.size = e["size"]
            info.type = {"file": tarfile.REGTYPE, "directory": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE}[e["type"]]
            info.linkname = e.get("target", "")
            if e["type"] == "file":
                with (output / e["path"]).open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    require(archive.stat().st_size <= MAX_ARCHIVE, "compressed archive byte limit")
    (output / "SHA256SUMS").write_text("".join(f"{digest(output / name)}  {name}\n" for name in PAYLOADS))
    return provenance


def archive_entries(archive, consume=None):
    """Read our bounded GNU tar subset, never tarfile.extract/all (including PAX parsing)."""
    require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= MAX_ARCHIVE, "archive size/type limit")
    entries = []
    long = {}
    total = 0
    with gzip.open(archive, "rb") as f:
        def read_exact(n):
            data = f.read(n)
            require(len(data) == n, "truncated archive")
            return data
        while True:
            block = read_exact(512)
            if block == bytes(512):
                require(not long and read_exact(512) == bytes(512), "invalid tar end")
                # tar writers pad to 10KiB; reject concatenated payloads and unbounded zero padding.
                tail = f.read(10241)
                require(len(tail) <= 10240 and not tail.strip(b"\0"), "trailing archive data")
                break
            info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
            require(0 <= info.size <= MAX_BYTES, "member size limit")
            if info.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                require(0 < info.size <= 4097 and info.type not in long, "oversized/duplicate long header")
                data = read_exact(info.size)
                require(data.endswith(b"\0") and b"\0" not in data[:-1], "invalid long header")
                long[info.type] = data[:-1].decode("utf-8")
                require(read_exact((-info.size) % 512) == bytes((-info.size) % 512), "invalid padding")
                continue
            require(info.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE), "hardlink/device/special/PAX member")
            name = long.pop(tarfile.GNUTYPE_LONGNAME, info.name)
            if info.isdir():
                name = name.removesuffix("/")
            target = long.pop(tarfile.GNUTYPE_LONGLINK, info.linkname)
            e = {"path": safe_path(name), "mode": info.mode, "size": info.size,
                 "type": "directory" if info.isdir() else "symlink" if info.issym() else "file"}
            require(info.mode & ~0o777 == 0 and not info.sparse, "special mode/sparse member")
            if e["type"] == "symlink":
                e["target"] = target
                link_path(name, target)
            require(e["type"] == "file" or info.size == 0, "non-file data")
            total += info.size
            require(total <= MAX_BYTES and len(entries) < MAX_MEMBERS, "archive expansion limit")
            h = hashlib.sha256()
            sink = consume(e) if consume else None
            try:
                remaining = info.size
                while remaining:
                    chunk = read_exact(min(remaining, 1024 * 1024))
                    remaining -= len(chunk)
                    h.update(chunk)
                    if sink:
                        sink.write(chunk)
            finally:
                if sink:
                    sink.close()
            require(read_exact((-info.size) % 512) == bytes((-info.size) % 512), "invalid file padding")
            if e["type"] == "file":
                e["sha256"] = h.hexdigest()
            entries.append(e)
    entries.sort(key=lambda e: e["path"])
    validate_entries(entries)
    return entries


def verify_bundle(bundle, expected_sha, workflow_sha, run_id, attempt):
    sha(expected_sha)
    sha(workflow_sha)
    sums = bundle / "SHA256SUMS"
    require(sums.is_file() and not sums.is_symlink() and sums.stat().st_size < 4096, "invalid SHA256SUMS")
    lines = sums.read_text().splitlines()
    require(len(lines) == len(PAYLOADS), "checksum inventory mismatch")
    for line, name in zip(lines, PAYLOADS):
        require(re.fullmatch(r"[0-9a-f]{64}  " + re.escape(name), line), "invalid checksum entry")
        path = bundle / name
        cap = MAX_ARCHIVE if name == "runtime.tar.gz" else MAX_METADATA
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= cap, "payload size/type limit")
        require(digest(path) == line[:64], "checksum mismatch")
    manifest = load_json(bundle / "file-manifest.json")
    p = load_json(bundle / "provenance.json")
    require(manifest["schema"] == p["schema"] == SCHEMA, "schema mismatch")
    require(p["source"]["repository"] == "pimmink/gsd-pi" and p["source"]["sha"] == expected_sha, "source pin mismatch")
    require(p["workflow"]["repository"] == "pimmink/gsd-pi-ci" and p["workflow"]["sha"] == workflow_sha and
            p["workflow"]["run_id"] == str(run_id) and p["workflow"]["attempt"] == str(attempt), "workflow/run pin mismatch")
    require(p["platform"]["os"] == "darwin" and p["platform"]["arch"] == "x64", "platform contract mismatch")
    require(p["manifest_sha256"] == digest(bundle / "file-manifest.json"), "manifest binding mismatch")
    validate_entries(manifest["entries"])
    entries = archive_entries(bundle / "runtime.tar.gz")
    require(entries == manifest["entries"], "archive manifest mismatch")
    require(p["closure"]["members"] == len(entries) and p["closure"]["bytes"] == sum(e["size"] for e in entries),
            "closure totals mismatch")
    dependencies = [e for e in entries if "node_modules" in e["path"].split("/")]
    require(p["closure"]["dependency_bytes"] == sum(e["size"] for e in dependencies) and
            p["closure"]["dependencies_sha256"] == hashlib.sha256(json_bytes(dependencies)).hexdigest(),
            "installed dependency fingerprint mismatch")
    require(p["closure"]["native"] in entries and p["closure"]["native"]["path"] == ADDON and
            p["closure"]["native"]["type"] == "file", "native binding mismatch")
    return p, entries


def extract_bundle(bundle, destination, expected_sha, workflow_sha, run_id, attempt):
    p, entries = verify_bundle(bundle, expected_sha, workflow_sha, run_id, attempt)
    require(not destination.exists() and not destination.is_symlink(), "destination must be new, never overlay")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    destination.mkdir(mode=0o700)
    # The complete graph and every content hash were checked BEFORE creating anything.
    for e in entries:
        path = destination / e["path"]
        if e["type"] == "directory":
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
    def consume(e):
        return (destination / e["path"]).open("xb") if e["type"] == "file" else None
    require(archive_entries(bundle / "runtime.tar.gz", consume) == entries, "archive changed during extraction")
    # Install links last, never traverse them while writing regular files.
    for e in entries:
        path = destination / e["path"]
        if e["type"] == "symlink":
            path.symlink_to(e["target"])
        else:
            path.chmod(e["mode"])
    require(inventory(destination / "runtime") == entries, "post-extraction inventory mismatch")
    for e in entries:
        if e["type"] == "symlink":
            require((destination / e["path"]).resolve(strict=True).is_relative_to(destination / "runtime"),
                    "extracted symlink escapes runtime")
    return p
