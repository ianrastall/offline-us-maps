"""Builds Shortbread map packages for regions Geofabrik publishes no package for: the
Canadian provinces and territories, and Alaska; and, with --world, the regions elsewhere listed
in to-build.json by tools/world-regions.py (China's provinces, Central America, Australia's
states, England's counties and others, and the parts of regions crossing the 180th meridian).
For publishing with the app's releases (tag `regions`) as <id>-shortbread-1.0.mbtiles.

Each package is made from Geofabrik's raw extract of the region with VersaTiles' Planetiler
Shortbread profile (versatiles-org/planetiler-shortbread v1.0.0-pre1, Apache-2.0; the release
jar, pinned by its SHA-256, in data/layer-build/tools): the same layers as Geofabrik's
packages, from the same data. Planetiler's bundled sample profile was not used: it writes no
boundary lines. Ocean polygons come from osmdata.openstreetmap.de (water-polygons-split-3857),
downloaded once. Alaska is clipped at the 180th meridian (its outline crosses it, which made
Geofabrik's package span the world); the Near Islands west of it are left out.

Stations, addresses and outdoor places for these regions come from Geofabrik's raw
extracts directly, so only the map packages are built here.

The data is (c) OpenStreetMap contributors, ODbL 1.0.

Usage: data/layer-build/venv/Scripts/python tools/regions-build/build_shortbread.py <out-dir> [id ...]
       data/layer-build/venv/Scripts/python tools/regions-build/build_shortbread.py <out-dir> --world <to-build.json> [catalog-id ...]
       (writes built.json, or built-world.json with --world, in <out-dir>)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# https://github.com/versatiles-org/planetiler-shortbread/releases/download/v1.0.0-pre1/planetiler-shortbread.jar
PLANETILER = ROOT / "data" / "layer-build" / "tools" / "planetiler-shortbread.jar"
PLANETILER_SHA256 = "e3ce25a8166c77ce257c3320e03822ecf1168c586e931ac12de4719590fdddf9"
WORK = ROOT / "data" / "regions-build"
GEOFABRIK = "https://download.geofabrik.de/north-america"

# Catalog ID: (name, Geofabrik path, bounds to clip to or None for the extract's own box)
REGIONS = {
    "ca-ab": ("Alberta", "canada/alberta", None),
    "ca-bc": ("British Columbia", "canada/british-columbia", None),
    "ca-mb": ("Manitoba", "canada/manitoba", None),
    "ca-nb": ("New Brunswick", "canada/new-brunswick", None),
    "ca-nl": ("Newfoundland and Labrador", "canada/newfoundland-and-labrador", None),
    "ca-ns": ("Nova Scotia", "canada/nova-scotia", None),
    "ca-nt": ("Northwest Territories", "canada/northwest-territories", None),
    # Nunavut whole is 3.0 GB, over GitHub's 2 GiB limit for a release file, so it is two
    # regions split at 90 degrees W: Kitikmeot and Kivalliq; Qikiqtaaluk.
    "ca-nu-w": ("Nunavut west of 90° W", "canada/nunavut", (-121.0, 51.0, -90.0, 85.0)),
    "ca-nu-e": ("Nunavut east of 90° W", "canada/nunavut", (-90.0, 51.0, -49.0, 85.0)),
    "ca-on": ("Ontario", "canada/ontario", None),
    "ca-pe": ("Prince Edward Island", "canada/prince-edward-island", None),
    "ca-qc": ("Quebec", "canada/quebec", None),
    "ca-sk": ("Saskatchewan", "canada/saskatchewan", None),
    "ca-yt": ("Yukon", "canada/yukon", None),
    "us-ak": ("Alaska", "us/alaska", (-180.0, 51.0, -129.9, 71.6)),
}


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def download(url: str, target: Path) -> None:
    """Downloads `url` to `target` unless a complete copy is there (same length)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "offline-maps-maintainer", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as r:
        length = int(r.headers["Content-Length"])
    if target.exists() and target.stat().st_size == length:
        return
    log(f"downloading {url} ({length / 1e6:.0f} MB)")
    partial = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "offline-maps-maintainer", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=120) as r, open(partial, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if partial.stat().st_size != length:
        raise RuntimeError(f"{url}: got {partial.stat().st_size} of {length} bytes")
    partial.replace(target)


def build(region_id: str, out: Path) -> dict:
    name, path, clip = REGIONS[region_id]
    return build_from(region_id, name, f"{GEOFABRIK}/{path}-latest.osm.pbf", clip, out, path)


def build_from(region_id: str, name: str, pbf_url: str, clip, out: Path, source: str, keep_source: bool = True) -> dict:
    sources = WORK / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    # Geofabrik's file names are unique except across continents' ID spaces; keep its path.
    pbf = sources / pbf_url.removeprefix("https://download.geofabrik.de/").replace("/", "_")
    download(pbf_url, pbf)
    target = out / f"{region_id}-shortbread-1.0.mbtiles"
    args = [
        "java", "-Xmx24g", "-jar", str(PLANETILER),
        f"--osm_path={pbf}", f"--output={target}", "--force",
        f"--ocean_path={sources / 'water-polygons-split-3857.zip'}", "--download",
        f"--tmpdir={WORK / 'tmp'}",
    ]
    if clip:
        args.append("--bounds=" + ",".join(str(v) for v in clip))
    log(f"building {region_id} ({name})")
    started = time.time()
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    db = sqlite3.connect(target)
    meta = dict(db.execute("SELECT name, value FROM metadata"))
    updates = {
        "name": f"Shortbread {name}",
        "description": f"{name}: Shortbread vector tiles built with Planetiler (VersaTiles profile) from Geofabrik's extract",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "license": "Open Database License 1.0",
        "author": "OpenStreetMap contributors; built for Offline Maps",
    }
    for key, value in updates.items():
        db.execute("DELETE FROM metadata WHERE name = ?", (key,))
        db.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", (key, value))
    db.commit()
    tiles = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
    db.close()
    size = target.stat().st_size
    log(f"  {target.name}: {tiles:,} tiles, {size / 1e6:.0f} MB, {time.time() - started:.0f} s, bounds {meta.get('bounds')}")
    record = {"id": region_id, "name": name, "geofabrik": source, "bounds": meta.get("bounds"),
              "package_bytes": size, "tiles": tiles, "pbf_bytes": pbf.stat().st_size}
    # The world's extracts are not kept: they come to about 11 GB and Geofabrik serves them again.
    # Both parts of a split region read one extract, so it stays until no later part needs it.
    if not keep_source:
        pbf.unlink(missing_ok=True)
    return record


def main() -> None:
    # Region names such as Jihočeský; Windows consoles and redirected output default to a code page.
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    with PLANETILER.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != PLANETILER_SHA256:
            raise RuntimeError("planetiler.jar does not match the pinned checksum")
    args = sys.argv[2:]
    if args[:1] == ["--world"]:
        entries = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        wanted = set(args[2:])
        listing_path = out / "built-world.json"
        listing = json.loads(listing_path.read_text(encoding="utf-8")) if listing_path.exists() else {}
        chosen = [e for e in entries if not wanted or e["catalog_id"] in wanted]
        for i, e in enumerate(chosen):
            later = any(f["geofabrik"] == e["geofabrik"] for f in chosen[i + 1:])
            listing[e["catalog_id"]] = build_from(
                e["catalog_id"], e["name"], e["url_base"] + "-latest.osm.pbf", e["clip"], out, e["geofabrik"],
                keep_source=later)
            listing_path.write_text(json.dumps(listing, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        log("done")
        return
    ids = args or list(REGIONS)
    listing_path = out / "built.json"
    listing = json.loads(listing_path.read_text(encoding="utf-8")) if listing_path.exists() else {}
    for region_id in ids:
        listing[region_id] = build(region_id, out)
        listing_path.write_text(json.dumps(listing, indent=1) + "\n", encoding="utf-8")
    log("done")


if __name__ == "__main__":
    main()
