"""Synthetic provider inputs for existing architecture tests; NOT release evidence.

Source-only temporary repos use real authenticated Git objects and physical bytes.
Provider Git/distribution hashes and tool metadata remain explicitly synthetic;
production still requires the exact real source files and real locked Git objects.
No runtime helper names are asserted to belong to real Sparkle/Sentry/Iroh releases.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import plistlib
import stat
import struct
from unittest.mock import patch
import zipfile

import native_cmux as native
import native_cmux_providers as providers

RESOURCE = "Versions/B/Resources/FixtureRequiredPayload"
REVISIONS = {"sparkle": "79bc9e872948e47877e76f194cb0c8e0412b0b90",
             "sentry-cocoa": "a24b98028b0b6e81bfd0526967468ba4ba60303c",
             "iroh-ffi": "ee19f156667ca640b912108a45f8b5bb8d156fec"}


def pin_source(case, source):
    """Real source-only Git fixture; provider Git/distributions remain synthetic."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], env=env,
                                       stderr=subprocess.PIPE).decode().strip()
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "owned synthetic source bytes", "--allow-empty")
    for item in (patch.object(native, "SOURCE_SHA", git("rev-parse", "HEAD")),
                 patch.object(native, "SOURCE_TREE", git("rev-parse", "HEAD^{tree}"))):
        item.start()
        case.addCleanup(item.stop)


def install(case):
    root = Path(case.temp.name).resolve() / "provider-inputs"
    source, packages = root / "source", root / "packages"
    source.mkdir(parents=True)
    packages.mkdir()
    archives, manifests = [], {}
    framework = case.app / "Contents/Frameworks/Sparkle.framework"
    # Valid structural header ONLY, fake tools still supply architecture/load data.
    # This is not a compiled or runnable Mach-O, and no SDK/API proof is claimed.
    (framework / "Versions/B/Sparkle").write_bytes(struct.pack('<IIIIIIII', 0xfeedfacf, 0x01000007, 3, 6, 0, 0, 0, 0) + b'UNIT-ONLY')
    (framework / RESOURCE).write_bytes(b"synthetic-required-runtime-resource")
    pins = []
    for identity, product in providers.PRODUCTS.items():
        checkout = packages / "checkouts" / identity
        checkout.mkdir(parents=True)
        library = "Sparkle.framework" if identity == "sparkle" else "FixtureStatic.a"
        target = "FixtureBinary"
        archive = root / (identity + ".zip")
        xcroot = identity + ".xcframework"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(xcroot + "/Info.plist", plistlib.dumps({"AvailableLibraries": [
                {"LibraryIdentifier": "macos-x86_64", "LibraryPath": library,
                 "SupportedPlatform": "macos", "SupportedArchitectures": ["x86_64"]},
                {"LibraryIdentifier": "ios-arm64", "LibraryPath": library,
                 "SupportedPlatform": "ios", "SupportedArchitectures": ["arm64"]}]}))
            prefix = xcroot + "/macos-x86_64/" + library
            if identity == "sparkle":
                for path in sorted(framework.rglob("*")):
                    relative = str(path.relative_to(framework))
                    info = zipfile.ZipInfo(prefix + "/" + relative + ("/" if path.is_dir() and not path.is_symlink() else ""))
                    info.create_system = 3
                    info.external_attr = path.lstat().st_mode << 16
                    if path.is_symlink():
                        z.writestr(info, path.readlink().as_posix().encode())
                    elif path.is_file():
                        z.writestr(info, path.read_bytes())
                    else:
                        z.writestr(info, b"")
                z.writestr(prefix + "/Headers/Fixture.h", b"development-only; omitted by embed")
            else:
                z.writestr(prefix, b"!<arch>\nSYNTHETIC-STATIC")
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest = (f'let package = Package(products: [.library(name: "{product}", targets: ["{target}"])], '
                    f'targets: [.binaryTarget(name: "{target}", url: "https://example.invalid/{identity}.zip", checksum: "{checksum}")])\n').encode()
        (checkout / "Package.swift").write_bytes(manifest)
        manifests[identity] = manifest
        archives.append(archive)
        pins.append({"identity": identity, "kind": "remoteSourceControl", "location": "https://example.invalid/" + identity,
                     "state": {"revision": REVISIONS[identity]}})
    lock = source / providers.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pins": pins}))
    configuration = source / "fixture-stock-debug-configuration"
    configuration.write_text("SYNTHETIC source producer/configuration; NOT a real stock receipt\n")
    hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (lock, configuration)}
    pin_source(case, source)

    def git_output(checkout, *args):
        checkout = Path(checkout)
        if checkout == source:
            return (native.SOURCE_SHA + ":" + native.SOURCE_TREE + "\n").encode()
        if args == ("show", "-s", "--format=%H", "HEAD"):
            return (REVISIONS[checkout.name] + "\n").encode()
        expected = ("show", REVISIONS[checkout.name] + ":Package.swift")
        if args != expected:
            raise AssertionError(args)
        return manifests[checkout.name]

    def git_blob(checkout, revision, path):
        identity = Path(checkout).name
        if revision != REVISIONS[identity] or path != 'Package.swift':
            raise ValueError('Synthetic fixture: wrong locked provider revision/path')
        return manifests[identity]

    # Existing graph tests create synthetic source-built dylibs. Classify them
    # as fixture producer outputs, NOT third-party provider exemptions. Production
    # source_owned_images remains the exact stock source product set; dedicated
    # F2 tests exercise the unpatched production classification as well.
    def fixture_source_products():
        return set(native.STOCK_PRODUCTS) | {str(p.relative_to(case.app)) for p in case.app.rglob('*.dylib')
                                            if not p.is_symlink()}

    for item in (patch.object(providers, "source_hashes", return_value=hashes),
                 patch.object(providers, "source_owned_images", side_effect=fixture_source_products),
                 patch.object(providers, "git_output", side_effect=git_output),
                 patch.object(providers, "git_blob", side_effect=git_blob)):
        item.start()
        case.addCleanup(item.stop)
    reference = case.app.parent / "native-cmux-providers.json"
    reference.write_text(json.dumps(providers.derive(source, packages, archives), indent=2))
    case.provider_reference, case.provider_source, case.provider_packages = reference, source, packages
    case.provider_archives, case.provider_manifests = archives, manifests
    return reference
