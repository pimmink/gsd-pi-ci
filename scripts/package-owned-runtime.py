#!/usr/bin/env python3
"""Runner-only packager: no install/build/start operations are hidden here."""
import argparse
import json
from pathlib import Path
from owned_runtime import package_runtime

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    try:
        p = package_runtime(args.source, args.output, json.loads(args.context.read_bytes()))
        print(json.dumps({"source_sha": p["source"]["sha"], "workflow_sha": p["workflow"]["sha"],
                          "bytes": p["closure"]["bytes"], "members": p["closure"]["members"]}))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"owned-runtime packaging refused: {exc}\n")
