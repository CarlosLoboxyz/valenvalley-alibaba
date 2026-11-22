#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.txt for the backend's runtime dependencies.

Walks backend/requirements.txt and every transitive dependency installed in the
CURRENT environment (via importlib.metadata), then writes name, version, license
and license text for each. Run it in an environment where requirements.txt was
installed (a fresh venv or the Docker image), not your global Python:

    python -m venv .venv && . .venv/bin/activate
    pip install -r backend/requirements.txt
    python tools/generate_notices.py            # writes THIRD_PARTY_NOTICES.txt
    python tools/generate_notices.py --check    # exit 1 on a disallowed license

Dev-only packages (requirements-dev.txt) are intentionally excluded: they are not
shipped. The --check mode fails on licenses outside the commercial-use allow-list
(i.e. GPL / AGPL / SSPL and unknown licenses).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.txt"

# SPDX ids (and legacy classifier names) acceptable for closed-source commercial use.
ALLOWED = {
    "mit", "isc", "bsd", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "apache",
    "0bsd", "zlib", "cc0-1.0", "psf", "psf-2.0", "python-2.0", "unlicense", "hpnd",
    "mpl-2.0",  # weak copyleft: OK while the files are used unmodified (certifi)
}
# Packages whose metadata is known-good but not machine-readable.
OVERRIDES = {
    "uvloop": "MIT OR Apache-2.0",
    "python-dateutil": "BSD-3-Clause OR Apache-2.0",
}

NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
LICENSE_FILE_RE = re.compile(r"(licen[sc]e|copying|notice)", re.I)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(line: str) -> tuple[str, set[str]] | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http")):
        return None
    m = NAME_RE.match(line)
    if not m:
        return None
    extras = {e.strip().lower() for e in (m.group(2) or "").split(",") if e.strip()}
    return norm(m.group(1)), extras


def direct_requirements() -> list[tuple[str, set[str]]]:
    out = []
    for line in REQUIREMENTS.read_text().splitlines():
        parsed = parse_requirement(line)
        if parsed:
            out.append(parsed)
    return out


def requires_of(dist: metadata.Distribution, extras: set[str]) -> list[tuple[str, set[str]]]:
    deps = []
    for req in dist.requires or []:
        spec, _, marker = req.partition(";")
        em = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
        if em and em.group(1).lower() not in extras:
            continue  # optional dependency that was not requested
        parsed = parse_requirement(spec)
        if parsed:
            deps.append(parsed)
    return deps


def license_of(dist: metadata.Distribution) -> str:
    name = norm(dist.metadata["Name"])
    if name in OVERRIDES:
        return OVERRIDES[name]
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    lic = (meta.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 80:
        return lic
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and "OSI Approved" not in c.split("::")[-1]
    ]
    if classifiers:
        return " OR ".join(classifiers)
    return "UNKNOWN"


def is_allowed(license_str: str) -> bool:
    ids = re.split(r"\s+(?:OR|AND|or|and)\s+|[()]|\s*/\s*", license_str)
    ids = [i.strip().lower() for i in ids if i.strip()]
    ids = [re.sub(r"\s+license$", "", i) for i in ids]
    ids = [re.sub(r"\s+license$", "", i.replace("osi approved ::", "").strip()) for i in ids]
    # Classifier-style names
    aliases = {"apache software license": "apache-2.0", "bsd license": "bsd", "mit license": "mit"}
    ids = [aliases.get(i, i) for i in ids]
    return bool(ids) and all(i in ALLOWED or i.removesuffix(" license") in ALLOWED for i in ids)


def license_texts(dist: metadata.Distribution) -> list[str]:
    texts = []
    for f in dist.files or []:
        if LICENSE_FILE_RE.search(f.name) and ".dist-info" in str(f):
            try:
                t = Path(dist.locate_file(f)).read_text(errors="replace").strip()
            except OSError:
                continue
            if t:
                texts.append(t)
    return texts


def collect() -> list[dict]:
    seen: dict[str, dict] = {}
    queue = list(direct_requirements())
    while queue:
        name, extras = queue.pop()
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            print(f"warning: {name} is not installed; skipped", file=sys.stderr)
            continue
        entry = seen.get(name)
        if entry and extras <= entry["extras"]:
            continue
        extras = extras | (entry["extras"] if entry else set())
        seen[name] = {
            "extras": extras,
            "name": dist.metadata["Name"],
            "version": dist.version,
            "license": license_of(dist),
            "home": dist.metadata.get("Home-page")
            or next(
                (
                    u.split(",", 1)[1].strip()
                    for u in dist.metadata.get_all("Project-URL") or []
                    if u.split(",", 1)[0].strip().lower() in {"homepage", "source", "repository"}
                ),
                "",
            ),
            "texts": license_texts(dist),
        }
        queue.extend(requires_of(dist, extras))
    return sorted(seen.values(), key=lambda p: p["name"].lower())


def render(pkgs: list[dict]) -> str:
    texts: dict[str, dict] = {}
    for p in pkgs:
        p["refs"] = []
        for t in p["texts"]:
            key = hashlib.sha256(" ".join(t.split()).encode()).hexdigest()[:12]
            texts.setdefault(key, {"text": t, "users": []})["users"].append(f"{p['name']}=={p['version']}")
            p["refs"].append(key)
    bar = "=" * 78
    out = [
        "THIRD-PARTY SOFTWARE NOTICES (backend)",
        "",
        "This product includes third-party open source software, listed below with",
        "its license. Generated by tools/generate_notices.py; do not edit by hand.",
        "",
        f"Packages: {len(pkgs)}",
        "",
        bar, "PACKAGES", bar, "",
    ]
    for p in pkgs:
        out.append(f"{p['name']}=={p['version']}")
        out.append(f"  License: {p['license']}")
        if p["home"]:
            out.append(f"  Source:  {p['home']}")
        if p["refs"]:
            out.append("  Text:    " + " ".join(f"[{k}]" for k in dict.fromkeys(p["refs"])))
        out.append("")
    out += [bar, "LICENSE TEXTS", bar, ""]
    for key, v in texts.items():
        out.append(f"[{key}] used by {len(v['users'])} package(s), e.g. {', '.join(v['users'][:3])}")
        out += ["-" * 78, v["text"], ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only verify licenses; do not write the file")
    args = ap.parse_args()

    pkgs = collect()
    bad = [p for p in pkgs if not is_allowed(p["license"])]
    if args.check:
        if bad:
            print("Packages with licenses outside the allow-list:", file=sys.stderr)
            for p in bad:
                print(f"  {p['name']}=={p['version']}: {p['license']}", file=sys.stderr)
            return 1
        print(f"OK: {len(pkgs)} packages, all licenses in the allow-list.")
        return 0

    OUTPUT.write_text(render(pkgs))
    print(f"Wrote {OUTPUT} ({len(pkgs)} packages).")
    for p in bad:
        print(f"WARNING: {p['name']}=={p['version']}: {p['license']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
