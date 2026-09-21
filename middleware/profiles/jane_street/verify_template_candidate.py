"""Frozen static evaluator for the one-field CMOS5L allocation candidate.

This is not an RTL simulator, area-fit test, or physical implementation check.
Only standard-library Python is needed. The baseline manifest is captured from
the actual working files, including staged and uncommitted changes.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evaluate(candidate: Path, manifest: dict) -> dict:
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    expected = manifest["files"]
    paths = {p.relative_to(candidate).as_posix(): p for p in candidate.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(candidate).parts}
    check("file_inventory", set(paths) == set(expected),
          {"added": sorted(set(paths) - set(expected)),
           "missing": sorted(set(expected) - set(paths))})
    changed = [name for name, digest in expected.items()
               if name != "info.yaml" and
               (name not in paths or sha256(paths[name].read_bytes()) != digest)]
    check("protected_sources_and_tests", not changed, changed)

    info_path = paths.get("info.yaml")
    if info_path is None:
        check("official_6x4_allocation", False, "info.yaml missing")
        check("only_authorized_configuration_delta", False, "info.yaml missing")
    else:
        info = info_path.read_bytes()
        tile_lines = re.findall(rb'^  tiles: "([^"\r\n]+)"[ \t]*\r?$', info, re.M)
        check("official_6x4_allocation", tile_lines == [b"6x4"],
              [v.decode("ascii", errors="replace") for v in tile_lines])
        # Exact-byte normalization preserves historical comments and formatting.
        # It rejects all changes besides this one predeclared field value.
        normalized, count = re.subn(rb'(?m)^  tiles: "6x4"(?=[ \t]*\r?$)',
                                    b'  tiles: "8x4"', info)
        check("only_authorized_configuration_delta",
              count == 1 and sha256(normalized) == expected["info.yaml"],
              {"tile_replacements": count,
               "normalized_sha256": sha256(normalized)})

    return {"evidence_level": "static_configuration_only",
            "passed": all(c["passed"] for c in checks), "checks": checks,
            "unrun": ["RTL simulation", "formal", "synthesis", "STA",
                      "place_and_route", "DRC_LVS", "FPGA", "silicon"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected_evaluator = manifest["evaluator_sha256"]
    if sha256(Path(__file__).read_bytes()) != expected_evaluator:
        parser.error("protected evaluator revision differs from the baseline manifest")
    result = evaluate(args.candidate.resolve(), manifest)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
