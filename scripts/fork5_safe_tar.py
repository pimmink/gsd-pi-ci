#!/usr/bin/env python3
"""Bounded, dependency-free tar safety helper for fork5-package-acceptance.mjs.

Invoked as a short-lived subprocess (spawnSync from Node, with a timeout) for
two jobs only:

  audit  <tarball>                       -> JSON member/link/device report
  extract <tarball> <destDir> <prefix...> -> extract ONLY members whose
                                             normalized relative path starts
                                             with one of the given prefixes,
                                             after rejecting absolute paths,
                                             '..' traversal, device/special
                                             members, and symlinks/hardlinks
                                             that escape the archive root
                                             either lexically (path algebra)
                                             or physically (realpath after
                                             extraction).

No third-party dependencies (stdlib `tarfile` only). Never shells out, never
calls extractall(), never trusts tarfile's own `data_filter`/`tar_filter`
alone (this double-checks explicitly so the caller does not depend on the
Python version's default extraction filter policy).
"""
import json
import os
import posixpath
import sys
import tarfile


def normalize_member_name(name):
    # Archive members from `npm pack` / `git archive` are POSIX paths.
    return posixpath.normpath(name.replace("\\", "/"))


def is_traversal(norm_name):
    return norm_name == ".." or norm_name.startswith("../") or "/../" in f"/{norm_name}/"


# Matches a Windows drive-letter absolute path (`C:\foo`, `C:/foo`, `c:foo`
# with drive-relative semantics is intentionally also rejected -- a bare
# `C:` prefix is ambiguous enough on a Windows host that we refuse it
# outright) and a UNC-style `\\server\share` path. Archive member names are
# POSIX strings but a hostile producer can still embed a Windows-absolute
# string; backslashes are normalized to forward slashes before this check
# runs so both separator styles are caught uniformly.
_WINDOWS_ABS_RE = __import__("re").compile(r"^(?:[A-Za-z]:[/\\]|[A-Za-z]:$|\\\\|//[^/])")


def is_windows_absolute(raw_name):
    normalized_seps = raw_name.replace("\\", "/")
    return bool(_WINDOWS_ABS_RE.match(raw_name)) or bool(_WINDOWS_ABS_RE.match(normalized_seps))


def is_absolute(name, norm_name):
    return name.startswith("/") or norm_name.startswith("/") or is_windows_absolute(name)


def classify_member(member):
    norm_name = normalize_member_name(member.name)
    finding = {
        "name": member.name,
        "normalized": norm_name,
        "isDevice": bool(member.isdev()),
        "isAbsolute": is_absolute(member.name, norm_name),
        "isTraversal": is_traversal(norm_name),
        "isSymlink": member.issym(),
        "isHardlink": member.islnk(),
        "linkTarget": member.linkname if (member.issym() or member.islnk()) else None,
        "escapingLink": False,
        "resolvedLinkTarget": None,
    }
    if finding["isSymlink"] or finding["isHardlink"]:
        target = member.linkname.replace("\\", "/")
        if target.startswith("/") or is_windows_absolute(member.linkname):
            finding["escapingLink"] = True
            finding["resolvedLinkTarget"] = target
        elif finding["isHardlink"]:
            # Per POSIX tar semantics (and Python's tarfile), a hardlink's
            # `linkname` is a path WITHIN THE ARCHIVE ROOT (i.e. relative to
            # the extraction root / the tar's own top-level namespace), NOT
            # relative to the directory containing the hardlink member
            # itself. Resolving it against `dirname(norm_name)` (as symlinks
            # correctly are) would under-detect an escape: a hardlink member
            # at `package/a/b/evil` with linkname `../../../etc/passwd`
            # that is actually meant as an archive-root-relative reference
            # must still be checked against the root, independent of its
            # own nesting depth.
            resolved = posixpath.normpath(target)
            finding["resolvedLinkTarget"] = resolved
            if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                finding["escapingLink"] = True
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(norm_name), target))
            finding["resolvedLinkTarget"] = resolved
            if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                finding["escapingLink"] = True
    return finding


def cmd_audit(tarball_path):
    findings = []
    with tarfile.open(tarball_path, mode="r:*") as tf:
        for member in tf.getmembers():
            findings.append(classify_member(member))
    print(json.dumps({"ok": True, "members": findings}))


def cmd_extract(tarball_path, dest_dir, prefixes):
    dest_real = os.path.realpath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    extracted = []
    skipped = []
    rejected = []
    with tarfile.open(tarball_path, mode="r:*") as tf:
        for member in tf.getmembers():
            finding = classify_member(member)
            norm_name = finding["normalized"]

            if finding["isAbsolute"]:
                rejected.append({**finding, "reason": "absolute-path"})
                continue
            if finding["isTraversal"]:
                rejected.append({**finding, "reason": "traversal"})
                continue
            if finding["isDevice"]:
                rejected.append({**finding, "reason": "device-special-file"})
                continue
            if finding["escapingLink"]:
                rejected.append({**finding, "reason": "escaping-link"})
                continue

            in_scope = any(
                norm_name == p.rstrip("/") or norm_name.startswith(p if p.endswith("/") else f"{p}/")
                for p in prefixes
            )
            if not in_scope:
                skipped.append({**finding, "reason": "out-of-scope"})
                continue

            # Lexical containment re-check immediately before extraction
            # (defense in depth even though classify_member already checked).
            target_path = os.path.join(dest_dir, norm_name)
            target_real_parent = os.path.realpath(os.path.dirname(target_path))
            if not (target_real_parent == dest_real or target_real_parent.startswith(dest_real + os.sep)):
                rejected.append({**finding, "reason": "lexical-containment-failed"})
                continue

            if member.isdir():
                os.makedirs(target_path, exist_ok=True)
                extracted.append(finding)
                continue
            if member.islnk():
                # Hardlink targets are archive-root-relative (see
                # classify_member), so the computed `resolvedLinkTarget` is
                # already a path relative to dest_dir's root, independent of
                # where the hardlink member itself is nested. Re-derive and
                # verify BOTH the lexical root-relative containment AND the
                # physical (realpath) containment of that target path
                # BEFORE calling tf.extract -- tarfile.extract() for a
                # LNKTYPE member creates the hard link by resolving linkname
                # against the extraction root itself, so an unvalidated
                # target here is a real pre-extraction escape vector, not
                # just a post-hoc cleanup case.
                hardlink_target_rel = finding["resolvedLinkTarget"]
                hardlink_target_path = os.path.join(dest_dir, hardlink_target_rel)
                hardlink_target_lexical_parent = os.path.realpath(os.path.dirname(hardlink_target_path))
                if not (
                    hardlink_target_lexical_parent == dest_real
                    or hardlink_target_lexical_parent.startswith(dest_real + os.sep)
                ):
                    rejected.append({**finding, "reason": "hardlink-target-lexical-containment-failed"})
                    continue
                if os.path.lexists(hardlink_target_path):
                    hardlink_target_real = os.path.realpath(hardlink_target_path)
                    if not (
                        hardlink_target_real == dest_real
                        or hardlink_target_real.startswith(dest_real + os.sep)
                    ):
                        rejected.append({**finding, "reason": "hardlink-target-physical-containment-failed"})
                        continue
                # Containment verified on both axes; extract via tarfile's
                # own member extraction (not extractall) so no other member
                # can influence this decision.
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                tf.extract(member, path=dest_dir, set_attrs=False)
            elif member.issym():
                # Symlinks were already escape-checked above (classify_member
                # resolves the link target relative to the member's own
                # directory, which is correct for symlinks per POSIX/tar
                # semantics). Extract via tarfile's own member extraction
                # (not extractall) so no other member can influence this.
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                tf.extract(member, path=dest_dir, set_attrs=False)
            elif member.isfile():
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                tf.extract(member, path=dest_dir, set_attrs=False)
            else:
                rejected.append({**finding, "reason": f"unsupported-member-type:{member.type!r}"})
                continue

            # Physical containment re-check AFTER extraction: resolve any
            # symlinks actually written to disk and confirm the real path is
            # still inside dest_real.
            if os.path.lexists(target_path):
                real_target = os.path.realpath(target_path)
                if not (real_target == dest_real or real_target.startswith(dest_real + os.sep)):
                    rejected.append({**finding, "reason": "physical-containment-failed-post-extract"})
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
                    continue
            extracted.append(finding)

    print(json.dumps({"ok": True, "extracted": extracted, "skipped": skipped, "rejected": rejected}))
    if rejected:
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: fork5_safe_tar.py <audit|extract> <tarball> [destDir prefix...]"}))
        sys.exit(1)
    mode = sys.argv[1]
    tarball_path = sys.argv[2]
    try:
        if mode == "audit":
            cmd_audit(tarball_path)
        elif mode == "extract":
            if len(sys.argv) < 5:
                print(json.dumps({"ok": False, "error": "extract requires destDir and at least one prefix"}))
                sys.exit(1)
            dest_dir = sys.argv[3]
            prefixes = sys.argv[4:]
            cmd_extract(tarball_path, dest_dir, prefixes)
        else:
            print(json.dumps({"ok": False, "error": f"unknown mode {mode!r}"}))
            sys.exit(1)
    except tarfile.TarError as e:
        print(json.dumps({"ok": False, "error": f"tarfile error: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
