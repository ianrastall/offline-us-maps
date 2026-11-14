"""Chooses the regions outside the US, Canada and Mexico for the catalog, and writes the inputs
tools/make-regions.py and tools/regions-build/build_shortbread.py need for them.

Candidates are the regions with no subregions in Geofabrik's index, outside North America
(Greenland excepted), leaving out groupings: regions mostly covered by smaller regions (dach,
alps, britain-and-ireland, great-britain, sea). A region with an enclave (Guangdong holding
Hong Kong and Macau, New South Wales the ACT) is not a grouping.

- Offered from Geofabrik (selected.json): candidates Geofabrik publishes a Shortbread package
  for, whose outline does not cross the 180th meridian.
- Built for the app (to-build.json): candidates without a package, and those crossing the 180th
  meridian, split there into a part west of 180 degrees and a part east of it, as Nunavut is
  split at 90 degrees W (Geofabrik builds such packages as if they spanned the world: Fiji
  4.8 GB, Russia's Far East 58 GB on 14 November 2026). Antarctica is left out: it spans every
  longitude and has almost no roads.

Overlapping outlines (Berlin inside Brandenburg's, the Azores inside Portugal's) are reported
only: the app merges overlapping regions, as it does All of Mexico with the Mexican states.

Catalog IDs are assigned here, offered regions first, so they stay the same from one run to
the next: a region's single ISO 3166-2 or 3166-1 code from the index (ge), else its country's
code and Geofabrik's ID (in-central-zone), else Geofabrik's ID (gcc-states); a code already
taken falls back to the next form. Parts add -w or -e (fj-w, fj-e).

Inputs (data/catalog/world/, written beforehand):
  index-v1.json      https://download.geofabrik.de/index-v1.json
  heads.txt          "<id> <status> [<bytes>]" per region: HEAD of its Shortbread package
  pbf.txt            "<id> <bytes>" per region: HEAD of its raw extract

Outputs (same folder), keyed by Geofabrik ID (with @w or @e for parts):
  selected.json, to-build.json  [{key, catalog_id, geofabrik, url_base, name, country,
                     country_iso, continent, iso, box, clip}] (clip: bounds for a part, or null)
  sizes.txt          Shortbread package sizes of selected regions
  pbf-sizes.txt      raw extract sizes of every region
  extract-boxes.txt  header box of each raw extract, by Geofabrik ID (64 KB range request each)
  boxes.txt          "<key> <west> <south> <east> <north>" outline boxes, for terrain-size

Usage: python -I tools/world-regions.py data/catalog/world [--no-fetch]
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

from shapely.geometry import box as make_box
from shapely.geometry import shape

HERE = Path(__file__).resolve().parent
NORTH_AMERICA_KEPT = {"greenland"}
LEFT_OUT = {"antarctica"}
# Parts of split regions that hold only open sea: Tuvalu's islands all lie west of 180 degrees;
# its outline merely reaches past the meridian.
PARTS_LEFT_OUT = {"tuvalu@e"}
# Groupings kept: in the catalog since 0.13.0 (South Africa alone has no package).
GROUPINGS_KEPT = {"south-africa-and-lesotho"}


def load_extract_boxes():
    spec = importlib.util.spec_from_file_location("extract_boxes", HERE / "extract-boxes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.header_bbox


def coords(geometry):
    out = []

    def walk(c):
        if isinstance(c[0], (int, float)):
            out.append((c[0], c[1]))
        else:
            for item in c:
                walk(item)

    walk(geometry["coordinates"])
    return out


def crosses_meridian(points):
    xs = [x for x, _ in points]
    return max(xs) - min(xs) > 180 or (min(xs) < -170 and max(xs) > 170)


def parts(points):
    """The part west of 180 degrees (longitudes 0 to 180) and the part east of it (-180 to 0),
    as clip bounds; a part whose points all lie within a degree of the meridian is dropped."""
    out = []
    for suffix, side in (("w", [(x, y) for x, y in points if x > 0]), ("e", [(x, y) for x, y in points if x < 0])):
        if not side or all(abs(x) > 179 for x, _ in side):
            continue
        xs, ys = [x for x, _ in side], [y for _, y in side]
        west, east = (min(xs), 180.0) if suffix == "w" else (-180.0, max(xs))
        out.append((suffix, [round(west, 4), round(min(ys), 4), round(east, 4), round(max(ys), 4)]))
    return out


def main():
    folder = Path(sys.argv[1])
    fetch = "--no-fetch" not in sys.argv
    index = json.loads((folder / "index-v1.json").read_text(encoding="utf-8"))
    features = {f["properties"]["id"]: f for f in index["features"]}
    parents = {f["properties"].get("parent") for f in index["features"]}
    shortbread = {}
    for line in (folder / "heads.txt").read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "200":
            shortbread[p[0]] = int(p[2])
    pbf = {}
    for line in (folder / "pbf.txt").read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) == 2:
            pbf[p[0]] = int(p[1])

    def chain(rid):
        out = []
        while rid:
            out.append(features[rid]["properties"])
            rid = features[rid]["properties"].get("parent")
        return out[::-1]  # continent first

    leaves = [r for r in features if r not in parents and r in pbf and r not in LEFT_OUT]
    candidates = [r for r in leaves
                  if chain(r)[0]["id"] != "north-america" or r in NORTH_AMERICA_KEPT]
    # Groupings: regions mostly covered by smaller regions.
    shapes = {r: shape(features[r]["geometry"]).buffer(0) for r in leaves}
    groupings = []
    for a in candidates:
        inner = [shapes[b] for b in leaves
                 if b != a and shapes[b].area < shapes[a].area and shapes[b].intersects(shapes[a])]
        covered = sum(shapes[a].intersection(b).area for b in inner)
        if covered > 0.5 * shapes[a].area and a not in GROUPINGS_KEPT:
            groupings.append(a)
    candidates = [r for r in candidates if r not in groupings]
    print("groupings left out: " + ", ".join(sorted(groupings)))

    def entry(rid, key=None, clip=None, suffix=""):
        f = features[rid]
        p = f["properties"]
        lineage = chain(rid)
        continent = lineage[0]
        country = lineage[1] if len(lineage) > 1 else lineage[0]
        country_iso = country.get("iso3166-1:alpha2") or [""]
        iso = p.get("iso3166-2") or p.get("iso3166-1:alpha2") or []
        points = coords(f["geometry"])
        whole = [min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)]
        # Geofabrik writes some names on two lines: "Vestlandet<br />(Western Norway)".
        name = re.sub(r"\s*<br\s*/?>\s*", " ", p["name"]).strip()
        if suffix:
            name += " west of 180°" if suffix == "w" else " east of 180°"
        return {
            "key": key or rid,
            "geofabrik": rid,
            "url_base": p["urls"]["pbf"].removesuffix("-latest.osm.pbf"),
            "name": name,
            "country": country["name"],
            "country_iso": country_iso[0] if len(country_iso) == 1 else "",
            "continent": continent["name"],
            "iso": iso[0] if len(iso) == 1 else "",
            "box": clip or whole,
            "clip": clip,
            "part": suffix,
        }

    selected, to_build = [], []
    for rid in candidates:
        points = coords(features[rid]["geometry"])
        if crosses_meridian(points):
            for suffix, clip in parts(points):
                if f"{rid}@{suffix}" not in PARTS_LEFT_OUT:
                    to_build.append(entry(rid, f"{rid}@{suffix}", clip, suffix))
        elif rid in shortbread:
            selected.append(entry(rid))
        else:
            to_build.append(entry(rid))
    # A region to build that an offered region already holds (South Africa, inside South
    # Africa and Lesotho) is left out.
    offered = [shapes[s["geofabrik"]] for s in selected]
    held = [s for s in to_build if not s["part"] and any(
        o.intersects(shapes[s["geofabrik"]]) and o.intersection(shapes[s["geofabrik"]]).area > 0.5 * shapes[s["geofabrik"]].area
        for o in offered)]
    to_build = [s for s in to_build if s not in held]
    print("held by an offered region, not built: " + ", ".join(s["geofabrik"] for s in held))
    order = lambda s: (s["continent"], s["country"], s["name"])
    selected.sort(key=order)
    to_build.sort(key=order)

    taken = set()
    for s in selected + to_build:
        country_iso = s["country_iso"].lower()
        candidates_ = [s["iso"].lower()] if s["iso"] else []
        candidates_ += [f"{country_iso}-{s['geofabrik']}"] if country_iso else []
        candidates_.append(s["geofabrik"])
        code = next(c for c in candidates_ if c and c not in taken)
        s["catalog_id"] = f"{code}-{s['part']}" if s["part"] else code
        taken.add(code if not s["part"] else s["catalog_id"])

    # Overlaps among offered and built regions, for review.
    keyed = {s["key"]: shapes[s["geofabrik"]] if not s["clip"] else shapes[s["geofabrik"]].intersection(make_box(*s["clip"]))
             for s in selected + to_build}
    for a in keyed:
        for b in keyed:
            if a != b and keyed[a].area <= keyed[b].area and keyed[a].intersects(keyed[b]):
                if keyed[a].intersection(keyed[b]).area > 0.5 * keyed[a].area:
                    print(f"overlap: {a} lies mostly inside {b}")

    def write(name, rows):
        (folder / name).write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    write("selected.json", selected)
    write("to-build.json", to_build)
    with open(folder / "sizes.txt", "w", encoding="utf-8", newline="\n") as out:
        out.writelines(f"{s['key']} {shortbread[s['key']]}\n" for s in selected)
    with open(folder / "pbf-sizes.txt", "w", encoding="utf-8", newline="\n") as out:
        out.writelines(f"{s['key']} {pbf[s['geofabrik']]}\n" for s in selected + to_build)
    with open(folder / "boxes.txt", "w", encoding="utf-8", newline="\n") as out:
        out.writelines(f"{s['key']} {' '.join(f'{v:.6f}' for v in s['box'])}\n" for s in selected + to_build)
    print(f"{len(selected)} regions offered from Geofabrik: maps {sum(shortbread[s['key']] for s in selected) / 1e9:.1f} GB")
    print(f"{len(to_build)} regions to build: raw extracts {sum(pbf[g] for g in {s['geofabrik'] for s in to_build}) / 1e9:.1f} GB")
    print("parts: " + ", ".join(f"{s['catalog_id']} {s['clip']}" for s in to_build if s["part"]))
    if fetch:
        header_bbox = load_extract_boxes()
        boxes = folder / "extract-boxes.txt"
        done = {line.split()[0] for line in boxes.read_text(encoding="utf-8").splitlines()} if boxes.exists() else set()
        with boxes.open("a", encoding="utf-8", newline="\n") as out:
            for gid in sorted({s["geofabrik"] for s in selected + to_build} - done):
                w, so, e, n = header_bbox(features[gid]["properties"]["urls"]["pbf"])
                out.write(f"{gid} {w:.6f} {so:.6f} {e:.6f} {n:.6f}\n")
                out.flush()
                print(f"extract box {gid}", flush=True)


if __name__ == "__main__":
    main()
