#!/usr/bin/env python3
"""
Parse FAA Coded Instrument Flight Procedures (CIFP) to extract all
Class B, C, and D controlled airspace boundaries.

Derives compact arc-encoded geometry via parameter recovery on WGS84
ellipsoid. This is significantly more sophisticated than direct extraction:
instead of using explicit FAA parameters directly, it reconstructs optimal
circles from the tessellated boundary vertices using geodesic fitting.

Note: the FAA's CIFP UC subsection only contains Class B/C/D airspace.
Class E (and lower) is not in this dataset and would need to come from
another source such as the FAA's NASR subscription.

Source: https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_YYMMDD.zip
Format: ARINC 424-18 fixed-width, 132 chars/line + CRLF
Records: Lines starting with "SUSAUC" (US controlled airspace boundaries)

Output: airspace.json with compact arc primitives: ["a", lon, lat, radius, bearing, sweep]
        for arcs; ["l", lon, lat] for straight segments; ["p", lon, lat, ...] for points.
"""

import argparse
import io
import json
import math
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
try:
    from geographiclib.geodesic import Geodesic
except ImportError:
    print("Error: geographiclib not found. Install with: pip install geographiclib", file=sys.stderr)
    sys.exit(1)


CIFP_URL = "https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_{cycle}.zip"
CIFP_ANCHOR = datetime(2026, 5, 14)


# ---------------------------------------------------------------------------
# Coordinate and altitude parsing
# ---------------------------------------------------------------------------

def parse_lat(s):
    """NDDMMSSSS (9 chars) -> decimal degrees. S hemisphere -> negative."""
    s = s.strip()
    hemi = s[0]
    val = int(s[1:3]) + int(s[3:5]) / 60.0 + int(s[5:9]) / 360000.0
    return -val if hemi == "S" else val


def parse_lon(s):
    """WDDDMMSSSS (10 chars) -> decimal degrees. W hemisphere -> negative."""
    s = s.strip()
    hemi = s[0]
    val = int(s[1:4]) + int(s[4:6]) / 60.0 + int(s[6:10]) / 360000.0
    return -val if hemi == "W" else val


def parse_altitude(s):
    """Return feet integer (MSL or AGL), or None on parse failure.

    The unit indicator (M/A) lives in a separate column and is handled by
    the caller; this function only handles the 5-char altitude value.

    GND / SFC -> 0
    UNL       -> 99999
    02500     -> 2500
    """
    s = s.strip()
    if not s or s in ("GND", "SFC"):
        return 0
    if s == "UNL":
        return 99999
    try:
        return int(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Download / extract
# ---------------------------------------------------------------------------

def download_cifp(cycle_yymmdd):
    url = CIFP_URL.format(cycle=cycle_yymmdd)
    print(f"Downloading {url} ...", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; FAA-CIFP-parser/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def extract_cifp18(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        target = next(
            (n for n in zf.namelist() if Path(n).name.upper().startswith("FAACIFP")),
            None,
        )
        if not target:
            raise ValueError(f"FAACIFP18 not found in zip. Contents: {zf.namelist()}")
        return zf.read(target).decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# ARINC 424-18 UC (Controlled Airspace) record column layout.
# All offsets are 0-based and inclusive of the start, exclusive of the end
# (Python slice convention).
#
# Common header (every UC record):
#   0:6    record id            "SUSAUC"
#   6:8    ICAO region          "K1", "K2", ...
#   8      airspace type        "A" (single airport), "T" (composite),
#                               "Z" (other)
#   9:13   airspace center      ICAO airport id, e.g. "KBOI"
#   14:16  section/subsection   "PA" (airport) or "HA" (heliport)
#   16     airspace class       "B", "C", or "D"
#   19     multiple code        shell letter A=innermost, B, C, D, ...
#   20:25  sequence number      "00100", "00200", ...
#   25     continuation         "0" for primary
#   30:32  boundary via         see below
#
# Boundary Via codes at columns 30:32:
#   "G "  great circle to next point
#   "GE"  great circle, last segment (closes polygon)
#   "H "  rhumb line to next point (treated same as G here)
#   "R "  clockwise arc to next point
#   "RE"  clockwise arc, last segment
#   "L "  counterclockwise arc to next point
#   "LE"  counterclockwise arc, last segment
#   "CE"  circle, complete (whole airspace is one circle)
#
# Geometry fields (presence depends on boundary via):
#   32:41  endpoint lat         NDDMMSSSS (9c) - for G/GE/H/R/RE/L/LE
#   41:51  endpoint lon         WDDDMMSSSS (10c) - same
#   51:60  arc/circle center lat (9c) - for R/RE/L/LE/CE
#   60:70  arc/circle center lon (10c) - same
#   70:74  arc distance (radius) in tenths of NM (4c) - for R/RE/L/LE/CE
#   74:78  arc bearing (4c)
#   78:81  RNP (3c)
#
# Altitudes (only on first record of each shell):
#   81:86  lower limit (5c)     "GND  ", "02500", "UNL  ", ...
#   86     lower unit indicator "M" = MSL, "A" = AGL
#   87:92  upper limit (5c)
#   92     upper unit indicator
#   93:123 airspace name (30c)
# ---------------------------------------------------------------------------

def parse_records(text):
    """Parse CIFP text into airspaces with arc-encoded geometry."""
    groups = {}  # (airspace_center, class_letter, shell_letter) -> [record, ...]

    for raw in text.splitlines():
        if not raw.startswith("SUSAUC"):
            continue
        line = raw.ljust(132)

        airspace_type   = line[8]
        airspace_center = line[9:13].strip()
        class_letter    = line[16]
        shell_letter    = line[19]
        seq_num         = line[20:25]
        boundary_via    = line[30:32]

        key = (airspace_type, airspace_center, class_letter, shell_letter)
        groups.setdefault(key, []).append({
            "line": line,
            "seq":  seq_num,
            "bv":   boundary_via,
        })

    airspaces = []
    for (atype, center, cls, shell), records in groups.items():
        records.sort(key=lambda r: r["seq"])

        first      = records[0]["line"]
        floor_ft   = parse_altitude(first[81:86])
        floor_unit = first[86]
        ceil_ft    = parse_altitude(first[87:92])
        ceil_unit  = first[92]
        name       = first[93:123].strip()

        geometry = _build_geometry(records)
        if geometry is None:
            continue

        airspaces.append({
            "id":             f"{atype}{center}_{cls}_{shell}",
            "name":           name,
            "icao":           center,
            "airspace_type":  atype,
            "class":          cls,
            "floor_ft":       floor_ft,
            "floor_ref":      "AGL" if floor_unit == "A" else "MSL",
            "ceiling_ft":     ceil_ft,
            "ceiling_ref":    "AGL" if ceil_unit == "A" else "MSL",
            "geometry":       geometry,
        })

    return airspaces


# ---------------------------------------------------------------------------
# Arc parameter recovery via geodesic fitting (WGS84 ellipsoid)
# ---------------------------------------------------------------------------

NM = 1852  # nautical mile to metres

STRAIGHT_RAD = 1e-4  # Threshold for classifying as straight line
TURN_REL_TOL = 0.1   # Relative tolerance for arc turn angles
MIN_RUN = 20         # Minimum run length for arc/line primitive

ARC_ACCEPT_M = 0.5   # Max error for accepting recovered arc (metres)
FIT_SAMPLES = 48     # Number of vertices to sample for fitting
RADIUS_SNAP_M = 1.0  # Tolerance for snapping to 0.1nm grid
POINTS_TOLERANCE_M = 0.25  # Simplification tolerance for point runs

COORD_PRECISION = 7

geodesic = Geodesic.WGS84


def _to_local_metres(ring):
    """Convert ring to local equirectangular metres about its centroid."""
    lon0 = sum(p[0] for p in ring) / len(ring)
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = math.cos(math.radians(lat0)) * 111320
    return [[(lon - lon0) * kx, (lat - lat0) * 110540] for lon, lat in ring]


def _turn_angles(points):
    """Calculate turn angle at each interior vertex."""
    turns = [0.0] * len(points)
    for i in range(1, len(points) - 1):
        ax, ay = points[i][0] - points[i-1][0], points[i][1] - points[i-1][1]
        bx, by = points[i+1][0] - points[i][0], points[i+1][1] - points[i][1]
        turns[i] = math.atan2(ax * by - ay * bx, ax * bx + ay * by)
    return turns


def _segment_ring(local_points):
    """Split ring into arc/line/point runs based on turn angles."""
    n = len(local_points)
    if n < MIN_RUN + 3:
        return [{"kind": "points", "start": 0, "end": n - 1}]

    turns = _turn_angles(local_points)
    runs = []

    cursor = 0
    i = 1
    while i < n - 1:
        reference = turns[i]
        tolerance = max(TURN_REL_TOL * abs(reference), 2e-5)
        j = i
        while j < n - 1 and abs(turns[j] - reference) <= tolerance:
            j += 1

        end = min(max(j, i + 1), n - 1)
        span = j - i
        if span >= MIN_RUN:
            kind = "arc" if abs(reference) >= STRAIGHT_RAD else "line"
        else:
            kind = "points"

        if runs and runs[-1]["kind"] == kind and runs[-1]["end"] == cursor:
            runs[-1]["end"] = end
        else:
            runs.append({"kind": kind, "start": cursor, "end": end})

        cursor = end
        i = j if j > i else i + 1

    if not runs or runs[-1]["end"] < n - 1:
        if runs and runs[-1]["kind"] == "points" and runs[-1]["end"] == cursor:
            runs[-1]["end"] = n - 1
        else:
            runs.append({"kind": "points", "start": cursor, "end": n - 1})

    return runs


def _sample_indices(start, end, count):
    """Sample evenly across [start, end]."""
    span = end - start
    if span <= count:
        return list(range(start, end + 1))
    return [start + round(k * span / (count - 1)) for k in range(count)]


def _planar_centre(points, indices):
    """Kasa algebraic circle fit in local metres (initial guess)."""
    Sx = Sy = Sxx = Syy = Sxy = Sxz = Syz = Sz = 0
    for i in indices:
        x, y = points[i]
        z = x * x + y * y
        Sx += x
        Sy += y
        Sxx += x * x
        Syy += y * y
        Sxy += x * y
        Sxz += x * z
        Syz += y * z
        Sz += z

    n = len(indices)
    m11 = 2 * (Sxx - Sx * Sx / n)
    m12 = 2 * (Sxy - Sx * Sy / n)
    m22 = 2 * (Syy - Sy * Sy / n)
    det = m11 * m22 - m12 * m12
    if abs(det) < 1e-9:
        return None

    r1 = Sxz - Sx * Sz / n
    r2 = Syz - Sy * Sz / n
    return [(r1 * m22 - r2 * m12) / det, (m11 * r2 - m12 * r1) / det]


def _geodesic_distance(centre, point):
    """Geodesic distance in metres from centre to point on WGS84."""
    result = geodesic.Inverse(centre[1], centre[0], point[1], point[0])
    return result["s12"]


def _refine_centre(ring_lonlat, indices, initial):
    """Refine centre via pattern search on geodesic radius spread."""
    def spread(centre):
        radii = [_geodesic_distance(centre, ring_lonlat[i]) for i in indices]
        return max(radii) - min(radii)

    centre = initial
    best = spread(centre)
    step = 0.005

    while step > 1e-9:
        improved = False
        for dx, dy in [(step, 0), (-step, 0), (0, step), (0, -step)]:
            candidate = [centre[0] + dx, centre[1] + dy]
            value = spread(candidate)
            if value < best:
                best = value
                centre = candidate
                improved = True
        if not improved:
            step /= 2

    return centre


def _bearing_from(centre, point):
    """Geodesic bearing from centre to point (degrees)."""
    result = geodesic.Inverse(centre[1], centre[0], point[1], point[0])
    return result["azi1"]


def _recover_arc(ring_lonlat, local_points, run):
    """Recover ["a", lon, lat, radius, bearing, sweep] or None."""
    indices = _sample_indices(run["start"], run["end"], FIT_SAMPLES)
    planar = _planar_centre(local_points, indices)
    if not planar:
        return None

    # Local metres back to lon/lat
    lon0 = sum(p[0] for p in ring_lonlat) / len(ring_lonlat)
    lat0 = sum(p[1] for p in ring_lonlat) / len(ring_lonlat)
    kx = math.cos(math.radians(lat0)) * 111320
    guess = [lon0 + planar[0] / kx, lat0 + planar[1] / 110540]

    centre = _refine_centre(ring_lonlat, indices, guess)

    # Average radius from sampled points
    radius = sum(_geodesic_distance(centre, ring_lonlat[i]) for i in indices) / len(indices)

    # Snap to 0.1nm grid if close enough
    snapped = round((radius / NM) * 10) / 10 * NM
    if abs(snapped - radius) <= RADIUS_SNAP_M:
        radius = snapped

    # Calculate bearing sweep
    start_bearing = _bearing_from(centre, ring_lonlat[run["start"]])
    previous = start_bearing
    sweep = 0.0

    for i in range(run["start"] + 1, run["end"] + 1):
        bearing = _bearing_from(centre, ring_lonlat[i])
        delta = bearing - previous
        while delta > 180:
            delta -= 360
        while delta < -180:
            delta += 360
        sweep += delta
        previous = bearing

    if abs(abs(sweep) - 360) < 0.5:
        sweep = 360 if sweep > 0 else -360

    arc = ["a", round(centre[0], COORD_PRECISION), round(centre[1], COORD_PRECISION),
           round(radius, 3), round(start_bearing, 6), round(sweep, 6)]

    # Validate: every source vertex must be within ARC_ACCEPT_M
    max_deviation = 0
    for i in range(run["start"], run["end"] + 1):
        offset = abs(_geodesic_distance([arc[1], arc[2]], ring_lonlat[i]) - radius)
        if offset > max_deviation:
            max_deviation = offset
            if max_deviation > ARC_ACCEPT_M:
                return None

    return {"arc": arc, "deviation": max_deviation}


def _segment_distance(p, a, b):
    """Point-to-segment distance in metres."""
    x, y = a
    dx, dy = b[0] - x, b[1] - y
    if dx != 0 or dy != 0:
        t = ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy)
        if t > 1:
            x, y = b
        elif t > 0:
            x += dx * t
            y += dy * t
    return math.hypot(p[0] - x, p[1] - y)


def _simplify_indices(points, run, tolerance):
    """Douglas-Peucker simplification, return kept indices."""
    first = run["start"]
    last = run["end"]
    if last - first <= 1:
        return [first, last]

    keep = [False] * (last - first + 1)
    keep[0] = True
    keep[last - first] = True

    stack = [[first, last]]
    while stack:
        lo, hi = stack.pop()
        worst = 0
        farthest = -1
        for i in range(lo + 1, hi):
            d = _segment_distance(points[i], points[lo], points[hi])
            if d > worst:
                worst = d
                farthest = i
        if farthest != -1 and worst > tolerance:
            keep[farthest - first] = True
            stack.extend([[lo, farthest], [farthest, hi]])

    return [first + i for i, k in enumerate(keep) if k]


def _encode_ring(ring):
    """Encode ring into primitives: arcs, lines, and point runs."""
    local_points = _to_local_metres(ring)
    runs = _segment_ring(local_points)
    primitives = []
    worst_deviation = 0

    for run in runs:
        if run["end"] <= run["start"]:
            continue

        if run["kind"] == "arc":
            recovered = _recover_arc(ring, local_points, run)
            if recovered:
                primitives.append(recovered["arc"])
                if recovered["deviation"] > worst_deviation:
                    worst_deviation = recovered["deviation"]
                continue
        elif run["kind"] == "line":
            # Validate that chord stands in for the line
            kept = [run["start"], run["end"]]
            deviation = max(abs(_segment_distance(local_points[i], local_points[run["start"]],
                                                   local_points[run["end"]]))
                          for i in range(run["start"] + 1, run["end"]))
            if deviation <= POINTS_TOLERANCE_M:
                primitives.append(["l", round(ring[run["end"]][0], COORD_PRECISION),
                                   round(ring[run["end"]][1], COORD_PRECISION)])
                if deviation > worst_deviation:
                    worst_deviation = deviation
                continue

        # Fallback to points with simplification
        kept = _simplify_indices(local_points, run, POINTS_TOLERANCE_M)
        if kept:
            flat = ["p"]
            for k in kept[1:]:
                flat.extend([round(ring[k][0], COORD_PRECISION), round(ring[k][1], COORD_PRECISION)])
            if len(flat) > 1:
                primitives.append(flat)

    return {
        "s": [round(ring[0][0], COORD_PRECISION), round(ring[0][1], COORD_PRECISION)],
        "d": primitives
    }


def _build_geometry(records):
    """Build compact arc-encoded geometry from CIFP records."""
    # Convert records to ring of [lon, lat] points
    points = []

    for r in records:
        bv = r["bv"]
        line = r["line"]
        code = bv[0]

        try:
            if code in ("G", "H", "R", "L"):
                # These all have an endpoint
                lon = parse_lon(line[41:51])
                lat = parse_lat(line[32:41])
                points.append([lon, lat])
            elif code == "C" and bv == "CE":
                # Full circle: extract center and radius, tessellate
                centre_lat = parse_lat(line[51:60])
                centre_lon = parse_lon(line[60:70])
                radius_nm = int(line[70:74]) / 10.0
                radius_m = radius_nm * NM

                # Tessellate at ~1 milliradian (similar to shapefile densification)
                steps = max(8, int(360 / (180 / math.pi / 1000)))
                for i in range(steps):
                    bearing = 360 * i / steps
                    result = geodesic.Direct(centre_lat, centre_lon, bearing, radius_m)
                    points.append([result["lon2"], result["lat2"]])
                break
        except (ValueError, IndexError):
            continue

    if not points or len(points) < 3:
        return None

    # Close the ring if needed
    if points[0] != points[-1]:
        points.append(points[0])

    return _encode_ring(points)


# ---------------------------------------------------------------------------
# Cycle date helpers
# ---------------------------------------------------------------------------

def current_cycle_yymmdd():
    today = datetime.now(tz=None).replace(tzinfo=None)
    days = (today - CIFP_ANCHOR).days
    return (CIFP_ANCHOR + timedelta(days=(days // 28) * 28)).strftime("%y%m%d")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Parse FAA CIFP airspace data into arc-encoded primitives via geodesic fitting."
    )
    ap.add_argument(
        "--cycle", metavar="YYMMDD",
        help="CIFP cycle date (default: auto-detect from today's date)",
    )
    ap.add_argument(
        "--cifp-file", metavar="FILE",
        help="Local CIFP .zip or FAACIFP18 text file (skips download)",
    )
    ap.add_argument(
        "--output", default="airspace.json",
        help="Output JSON path (default: airspace.json)",
    )
    args = ap.parse_args()

    cycle = args.cycle or current_cycle_yymmdd()
    print(f"Cycle: {cycle}", file=sys.stderr)

    if args.cifp_file:
        p = Path(args.cifp_file)
        text = (extract_cifp18(p.read_bytes()) if p.suffix.lower() == ".zip"
                else p.read_text("ascii", errors="replace"))
    else:
        text = extract_cifp18(download_cifp(cycle))

    airspaces = parse_records(text)

    counts = {}
    for a in airspaces:
        counts[a["class"]] = counts.get(a["class"], 0) + 1

    result = {
        "source":    f"FAA CIFP 28-day cycle {cycle}",
        "format":    "arc-encoded primitives (geodesic fitting on WGS84)",
        "scope":     "All Class B/C/D airspace shells (CIFP UC subsection)",
        "count":     len(airspaces),
        "by_class":  counts,
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "airspaces": airspaces,
    }

    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"Wrote {len(airspaces)} airspaces to {args.output}", file=sys.stderr)
    for cls in sorted(counts):
        print(f"  Class {cls}: {counts[cls]}", file=sys.stderr)
    print(f"\nFormat: compact arc-encoded primitives", file=sys.stderr)
    print(f"  Arcs:  [\"a\", lon, lat, radius_metres, start_bearing, sweep_degrees]", file=sys.stderr)
    print(f"  Lines: [\"l\", lon, lat]", file=sys.stderr)
    print(f"  Points: [\"p\", lon, lat, lon, lat, ...]", file=sys.stderr)


if __name__ == "__main__":
    main()
