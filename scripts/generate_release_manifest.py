"""Generate deterministic SHA256SUMS.txt and CycloneDX 1.5 SBOM for Kryptos Core releases."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import pathlib
import sys
import uuid
from datetime import UTC, datetime
from typing import Any


def compute_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def generate_sha256sums(artifacts_dir: pathlib.Path, out_file: pathlib.Path | None = None) -> pathlib.Path:
    """Write `<sha256>  <filename>` entries for all regular release artifacts in `artifacts_dir`."""
    artifacts_dir = artifacts_dir.resolve()
    target = out_file.resolve() if out_file else (artifacts_dir / "SHA256SUMS.txt")
    lines: list[str] = []
    if artifacts_dir.is_dir():
        for p in sorted(artifacts_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name in ("SHA256SUMS.txt", "sbom.cyclonedx.json"):
                continue
            lines.append(f"{compute_sha256(p)}  {p.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


def build_cyclonedx_sbom(repo_root: pathlib.Path, version: str = "3.0.0") -> dict[str, Any]:
    """Build a CycloneDX 1.5 JSON SBOM covering Python and frontend dependencies."""
    components: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for dist in sorted(importlib.metadata.distributions(), key=lambda d: str(d.metadata.get("Name") or "").lower()):
        name = str(dist.metadata.get("Name") or "").strip()
        ver = str(dist.version or "0.0.0").strip()
        if not name:
            continue
        key = ("pypi", name.lower())
        if key in seen:
            continue
        seen.add(key)
        components.append(
            {
                "type": "library",
                "name": name,
                "version": ver,
                "purl": f"pkg:pypi/{name}@{ver}",
            }
        )

    pkg_json = repo_root / "workspaces" / "offline-chat" / "web" / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = data.get("dependencies") or {}
            if isinstance(deps, dict):
                for name, raw_ver in sorted(deps.items()):
                    ver = str(raw_ver).lstrip("^~>=< ")
                    key = ("npm", str(name).lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    components.append(
                        {
                            "type": "library",
                            "name": str(name),
                            "version": ver,
                            "purl": f"pkg:npm/{name}@{ver}",
                        }
                    )
        except (OSError, ValueError):
            pass

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "name": "kryptos-core",
                "version": version,
            },
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SHA256SUMS.txt and CycloneDX SBOM.")
    parser.add_argument("--artifacts-dir", required=True, help="Directory containing built release artifacts")
    parser.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parents[1]), help="Repository root")
    parser.add_argument("--version", default="3.0.0", help="Release version string")
    args = parser.parse_args(argv)

    artifacts_dir = pathlib.Path(args.artifacts_dir).resolve()
    repo_root = pathlib.Path(args.repo_root).resolve()
    sums_path = generate_sha256sums(artifacts_dir)
    sbom = build_cyclonedx_sbom(repo_root, version=args.version)
    sbom_path = artifacts_dir / "sbom.cyclonedx.json"
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {sums_path.name} and {sbom_path.name} ({len(sbom['components'])} SBOM components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
