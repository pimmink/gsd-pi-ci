#!/usr/bin/env python3
"""Verify all bytes before extraction. Optional native/import proof executes reviewed code."""
import argparse
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from owned_runtime import digest, extract_bundle, inventory, load_json, require, verify_bundle

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--destination", type=Path, help="must not exist; its parent must exist")
    parser.add_argument("--probe-node", type=Path, help="absolute Node 24.15+ executable; executes only import/native probe, not CLI host")
    args = parser.parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        pins = (args.expected_sha, args.workflow_sha, args.run_id, args.attempt)
        if args.probe_node and (not args.destination or not args.probe_node.is_absolute()):
            parser.error("--probe-node requires --destination and an absolute executable")
        if args.destination:
            p = extract_bundle(args.bundle, args.destination, *pins)
        else:
            p, _ = verify_bundle(args.bundle, *pins)
        result = {"schema": "gsd-owned-runtime-verification-v1", "source": p["source"], "workflow": p["workflow"],
                  "archive_sha256": digest(args.bundle / "runtime.tar.gz"), "all_bytes_verified": True,
                  "started_at": started_at,
                  "extracted": bool(args.destination), "runtime_probe": None}
        if args.probe_node:
            owned = args.destination.resolve()
            state = owned / "probe-state"
            state.mkdir()
            env = {"PATH": str(args.probe_node.parent) + ":/usr/bin:/bin", "HOME": str(state),
                   "GSD_HOME": str(state / "gsd"), "GSD_CODING_AGENT_DIR": str(state / "agent"),
                   "TMPDIR": str(state), "XDG_CONFIG_HOME": str(state / "config"),
                   "XDG_CACHE_HOME": str(state / "cache"), "NODE_COMPILE_CACHE": str(state / "node-cache"),
                   "GSD_NATIVE_PREFER_LOCAL": "1", "GSD_SKIP_DEP_REPAIR": "1", "GSD_SKIP_RTK_INSTALL": "1"}
            output = subprocess.check_output(
                [str(args.probe_node), "--experimental-import-meta-resolve", str(Path(__file__).with_name("probe-owned-runtime.mjs")),
                 str(owned / "runtime"), str(args.bundle.resolve() / "provenance.json")],
                env=env, cwd=state, text=True, timeout=90)
            result["runtime_probe"] = json.loads(output)
            require(inventory(owned / "runtime") == load_json(args.bundle / "file-manifest.json")["entries"],
                    "runtime changed during import/native probe")
        result["verified_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(result, sort_keys=True, indent=2))
    except (ValueError, KeyError, OSError, EOFError, tarfile.TarError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"owned-runtime verification refused: {exc}\n")
