"""
Reconstruct geometric primitives (circles, arcs, straight lines) from the
FAA NASR Class Airspace shapefile, which tessellates everything as polygons.

Strategy:
  1. For each polygon, project to a local flat plane scaled to nautical miles.
  2. Use a sliding-window least-squares circle fit (Kasa method).
  3. Greedy expansion: a run of consecutive vertices forms an arc if a single
     center fits all of them to <0.05 NM and their angular spacing is monotonic.
  4. If a closed loop is one arc spanning ~360 deg, emit a circle.
  5. Remaining vertices become line segments. Adjacent line vertices that are
     nearly collinear get merged.
  6. Snap arc/circle radii to nearest 0.5 NM if within tolerance (FAA convention).
"""

import json
import math
import shapefile
from pathlib import Path

# --- tunables ---
ARC_FIT_TOL_NM = 0.08        # max residual for a vertex to belong to an arc
MIN_ARC_POINTS = 12          # below this, call it lines
SEED_ARC_POINTS = 32         # larger seed window helps avoid latching onto noise
LINE_COLLINEAR_TOL_NM = 0.02 # merge near-collinear line vertices
MIN_ARC_RADIUS_NM = 0.5      # reject "arcs" smaller than this — they're corner artifacts
MIN_ARC_SWEEP_DEG = 3.0      # an arc must sweep at least this much — else it's just a line
RADIUS_SNAP_TOL_NM = 0.15    # snap fitted radius to half-NM if within
FULL_CIRCLE_THRESH_DEG = 350 # arc spanning this much = full circle

NM_PER_DEG_LAT = 60.0


def project(points, lat0, lon0):
    """Equirectangular projection to NM, centered at (lat0, lon0)."""
    coslat = math.cos(math.radians(lat0))
    return [((lon - lon0) * NM_PER_DEG_LAT * coslat,
             (lat - lat0) * NM_PER_DEG_LAT) for lon, lat in points]


def unproject(x, y, lat0, lon0):
    coslat = math.cos(math.radians(lat0))
    return (lon0 + x / (NM_PER_DEG_LAT * coslat),
            lat0 + y / NM_PER_DEG_LAT)


def fit_circle_kasa(pts):
    """Algebraic circle fit. Returns (cx, cy, r, max_residual)."""
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0]*p[0] for p in pts)
    syy = sum(p[1]*p[1] for p in pts)
    sxy = sum(p[0]*p[1] for p in pts)
    sxxx = sum(p[0]**3 for p in pts)
    syyy = sum(p[1]**3 for p in pts)
    sxyy = sum(p[0]*p[1]*p[1] for p in pts)
    sxxy = sum(p[0]*p[0]*p[1] for p in pts)

    # Solve linear system from algebraic circle equation
    A = [[sxx, sxy, sx],
         [sxy, syy, sy],
         [sx,  sy,  n]]
    B = [-(sxxx + sxyy),
         -(sxxy + syyy),
         -(sxx + syy)]

    det = (A[0][0]*(A[1][1]*A[2][2] - A[1][2]*A[2][1]) -
           A[0][1]*(A[1][0]*A[2][2] - A[1][2]*A[2][0]) +
           A[0][2]*(A[1][0]*A[2][1] - A[1][1]*A[2][0]))
    if abs(det) < 1e-12:
        return None

    def cofactor_solve(idx):
        M = [row[:] for row in A]
        for r in range(3):
            M[r][idx] = B[r]
        return (M[0][0]*(M[1][1]*M[2][2] - M[1][2]*M[2][1]) -
                M[0][1]*(M[1][0]*M[2][2] - M[1][2]*M[2][0]) +
                M[0][2]*(M[1][0]*M[2][1] - M[1][1]*M[2][0])) / det

    D = cofactor_solve(0)
    E = cofactor_solve(1)
    F = cofactor_solve(2)
    cx = -D / 2
    cy = -E / 2
    rsq = cx*cx + cy*cy - F
    if rsq <= 0:
        return None
    r = math.sqrt(rsq)
    max_res = max(abs(math.hypot(p[0]-cx, p[1]-cy) - r) for p in pts)
    return (cx, cy, r, max_res)


def angle_at(pt, cx, cy):
    return math.degrees(math.atan2(pt[1]-cy, pt[0]-cx))


def angular_span(angles):
    """Sweep distance covered by a list of angles, accounting for wrap.
    Returns (span_deg, direction) where direction is +1 CCW, -1 CW."""
    if len(angles) < 2:
        return 0, 1
    diffs = []
    for i in range(len(angles)-1):
        d = angles[i+1] - angles[i]
        # normalize to [-180, 180]
        while d > 180: d -= 360
        while d < -180: d += 360
        diffs.append(d)
    total = sum(diffs)
    direction = 1 if total >= 0 else -1
    # Check monotonicity: all diffs same sign (allowing tiny opposite)
    same_sign = all(d * direction >= -0.01 for d in diffs)
    return abs(total), direction, same_sign


def reconstruct_ring(ring_points):
    """ring_points: list of (lon, lat) tuples forming a closed ring.
    Returns: list of segments. Each segment is one of:
      {"type": "circle", "center": [lon,lat], "radius_nm": r}
      {"type": "arc", "center": [lon,lat], "radius_nm": r,
       "start": [lon,lat], "end": [lon,lat],
       "start_bearing_deg": b1, "end_bearing_deg": b2,
       "direction": "CW"|"CCW"}
      {"type": "line", "from": [lon,lat], "to": [lon,lat]}
    """
    if len(ring_points) < 4:
        return []

    # Drop duplicate closing vertex if present
    if ring_points[0] == ring_points[-1]:
        ring_points = ring_points[:-1]

    n = len(ring_points)
    # Local projection origin = centroid
    lat0 = sum(p[1] for p in ring_points) / n
    lon0 = sum(p[0] for p in ring_points) / n
    proj = project(ring_points, lat0, lon0)

    # --- Step 1: Greedy arc extraction with corrected expansion ---
    used = [False] * n
    arcs = []  # list of (start_idx, end_idx, cx, cy, r, direction, span)

    def try_arc(start, length):
        """Return fit tuple if `length` points starting at start form a valid arc.
        All points within window must be unused."""
        if length < MIN_ARC_POINTS or length > n:
            return None
        for k in range(length):
            if used[(start + k) % n]:
                return None
        window = [proj[(start + k) % n] for k in range(length)]
        fit = fit_circle_kasa(window)
        if fit is None or fit[3] > ARC_FIT_TOL_NM:
            return None
        cx, cy, r, _ = fit
        if r > 200 or r < MIN_ARC_RADIUS_NM:
            return None
        angles = [angle_at(p, cx, cy) for p in window]
        span, direction, mono = angular_span(angles)
        if not mono:
            return None
        return (cx, cy, r, direction, span)

    i = 0
    while i < n:
        if used[i]:
            i += 1
            continue
        seed = try_arc(i, SEED_ARC_POINTS)
        if seed is None:
            i += 1
            continue
        last_good = seed
        # Determine max unused contiguous length from i
        max_len = 0
        for k in range(n):
            if used[(i + k) % n]:
                break
            max_len += 1
        # Exponential growth: keep doubling until fit fails or we hit max_len
        lo = SEED_ARC_POINTS
        cur = SEED_ARC_POINTS
        upper_bound = max_len + 1  # sentinel meaning we got all the way to max_len
        while cur < max_len:
            nxt = min(cur * 2, max_len)
            if nxt == cur:
                break
            fit = try_arc(i, nxt)
            if fit is None:
                upper_bound = nxt
                break
            last_good = fit
            lo = nxt
            cur = nxt
            if cur == max_len:
                break
        # Binary search between lo (success) and upper_bound (failure)
        if upper_bound <= max_len:
            hi = upper_bound
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                fit = try_arc(i, mid)
                if fit is None:
                    hi = mid
                else:
                    lo = mid
                    last_good = fit
        cx, cy, r, direction, span = last_good
        if span < MIN_ARC_SWEEP_DEG:
            i += 1
            continue
        end = i + lo - 1
        for k in range(i, end + 1):
            used[k % n] = True
        arcs.append((i, end, cx, cy, r, direction, span))
        i = end + 1

    # --- Step 2: Convert arcs spanning ~360 deg into circles ---
    segments = []

    # Reconstruct sequence of (type, data) walking around the ring
    # Build per-index ownership: which arc each index belongs to (or None)
    owner = [None] * n
    for aidx, (start, end, cx, cy, r, direction, span) in enumerate(arcs):
        for k in range(start, end + 1):
            owner[k % n] = aidx

    # Walk the ring producing segments in order
    visited_arcs = set()
    k = 0
    line_buf = []

    def flush_line_buf():
        if len(line_buf) < 2:
            return
        # Optionally merge nearly-collinear segments
        merged = [line_buf[0]]
        for j in range(1, len(line_buf) - 1):
            a, b, c = line_buf[j-1], line_buf[j], line_buf[j+1]
            # cross product distance from b to line ac, in NM
            ax, ay = proj[a]
            bx, by = proj[b]
            cx_, cy_ = proj[c]
            num = abs((cx_ - ax) * (ay - by) - (ax - bx) * (cy_ - ay))
            den = math.hypot(cx_ - ax, cy_ - ay)
            d = num / den if den > 0 else 0
            if d > LINE_COLLINEAR_TOL_NM:
                merged.append(b)
        merged.append(line_buf[-1])
        for j in range(len(merged) - 1):
            segments.append({
                "type": "line",
                "from": [round(ring_points[merged[j]][0], 6),
                         round(ring_points[merged[j]][1], 6)],
                "to":   [round(ring_points[merged[j+1]][0], 6),
                         round(ring_points[merged[j+1]][1], 6)],
            })
        line_buf.clear()

    while k < n:
        if owner[k] is not None and owner[k] not in visited_arcs:
            aidx = owner[k]
            visited_arcs.add(aidx)
            flush_line_buf()
            start, end, cx, cy, r, direction, span = arcs[aidx]
            center_ll = unproject(cx, cy, lat0, lon0)
            # Snap radius
            radius = r
            nearest_half = round(r * 2) / 2
            if abs(r - nearest_half) < RADIUS_SNAP_TOL_NM:
                radius = nearest_half

            if span >= FULL_CIRCLE_THRESH_DEG:
                segments.append({
                    "type": "circle",
                    "center": [round(center_ll[0], 6), round(center_ll[1], 6)],
                    "radius_nm": round(radius, 3),
                })
            else:
                start_pt = ring_points[start % n]
                end_pt = ring_points[end % n]
                start_brg = (90 - angle_at(proj[start % n], cx, cy)) % 360
                end_brg = (90 - angle_at(proj[end % n], cx, cy)) % 360
                segments.append({
                    "type": "arc",
                    "center": [round(center_ll[0], 6), round(center_ll[1], 6)],
                    "radius_nm": round(radius, 3),
                    "start": [round(start_pt[0], 6), round(start_pt[1], 6)],
                    "end":   [round(end_pt[0], 6), round(end_pt[1], 6)],
                    "start_bearing_deg": round(start_brg, 2),
                    "end_bearing_deg": round(end_brg, 2),
                    "direction": "CCW" if direction > 0 else "CW",
                    "sweep_deg": round(span, 2),
                })
            k = end + 1
        else:
            line_buf.append(k)
            k += 1

    flush_line_buf()
    segments = coalesce_segments(segments, lat0, lon0)
    # If the ring ended on a line, connect last buffered point back to first non-arc vertex
    # (Most rings close naturally because we use modular indexing on arcs.)

    return segments


def coalesce_segments(segments, lat0, lon0):
    """Merge adjacent arc segments that share approximately the same center and radius."""
    if not segments:
        return segments
    CENTER_TOL_NM = 0.15
    RADIUS_TOL_NM = 0.15
    coslat = math.cos(math.radians(lat0))

    def ll_dist_nm(a, b):
        return math.hypot((a[0]-b[0])*60*coslat, (a[1]-b[1])*60)

    merged = [segments[0]]
    for s in segments[1:]:
        prev = merged[-1]
        if (prev["type"] == "arc" and s["type"] == "arc"
                and prev.get("direction") == s.get("direction")
                and abs(prev["radius_nm"] - s["radius_nm"]) < RADIUS_TOL_NM
                and ll_dist_nm(prev["center"], s["center"]) < CENTER_TOL_NM):
            # Combine: take prev start, s end, sum sweeps
            prev["end"] = s["end"]
            prev["end_bearing_deg"] = s["end_bearing_deg"]
            prev["sweep_deg"] = round(prev["sweep_deg"] + s["sweep_deg"], 2)
            # If the combined sweep is now nearly a full circle, demote to circle
            if prev["sweep_deg"] >= FULL_CIRCLE_THRESH_DEG:
                merged[-1] = {
                    "type": "circle",
                    "center": prev["center"],
                    "radius_nm": prev["radius_nm"],
                }
        else:
            merged.append(s)
    return merged



def reconstruct_record(shape, record):
    parts = list(shape.parts) + [len(shape.points)]
    rings = []
    for pi in range(len(parts) - 1):
        ring = list(shape.points[parts[pi]:parts[pi+1]])
        if len(ring) >= 4:
            rings.append(reconstruct_ring(ring))
    return rings


NASR_ANCHOR = (2026, 5, 14)  # known good cycle date to anchor 28-day math
SHAPEFILE_URL = "https://nfdc.faa.gov/webContent/28DaySub/{date}/class_airspace_shape_files.zip"


def current_cycle_date():
    """Return the most recent NASR 28-day cycle effective date."""
    import datetime
    anchor = datetime.date(*NASR_ANCHOR)
    today = datetime.date.today()
    days_since = (today - anchor).days
    offset = (days_since // 28) * 28
    return anchor + datetime.timedelta(days=offset)


def download_shapefile(cycle_date, dest_dir):
    """Download and unpack the class airspace shapefile for a given cycle date."""
    import urllib.request, zipfile, io, os
    url = SHAPEFILE_URL.format(date=cycle_date)
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest_dir)
    # FAA nests inside Shape_Files/
    shp_dir = os.path.join(dest_dir, "Shape_Files")
    if not os.path.isdir(shp_dir):
        raise FileNotFoundError(f"Expected Shape_Files/ inside zip, got: {os.listdir(dest_dir)}")
    return shp_dir


def process_shapefile(shp_dir, cycle_date):
    sf = shapefile.Reader(str(Path(shp_dir) / "Class_Airspace"))
    out = []
    for i in range(len(sf)):
        rec = sf.record(i).as_dict()
        klass = rec.get("CLASS", "")
        if klass not in ("B", "C", "D"):
            continue
        rings = reconstruct_record(sf.shape(i), rec)
        out.append({
            "id": rec.get("IDENT", "") or f"CLS_{i}",
            "name": rec.get("NAME", ""),
            "class": klass,
            "type_code": rec.get("TYPE_CODE", ""),
            "local_type": rec.get("LOCAL_TYPE", ""),
            "upper_value": rec.get("UPPER_VAL", ""),
            "upper_uom":   rec.get("UPPER_UOM", ""),
            "upper_code":  rec.get("UPPER_CODE", ""),
            "upper_desc":  rec.get("UPPER_DESC", ""),
            "lower_value": rec.get("LOWER_VAL", ""),
            "lower_uom":   rec.get("LOWER_UOM", ""),
            "lower_code":  rec.get("LOWER_CODE", ""),
            "comm_name":   rec.get("COMM_NAME", ""),
            "mil_code":    rec.get("MIL_CODE", ""),
            "level":       rec.get("LEVEL", ""),
            "working_hours_code": rec.get("WKHR_CODE", ""),
            "working_hours_remark": rec.get("WKHR_RMK", ""),
            "rings": rings,
        })

    result = {
        "source": f"FAA NASR 28-day cycle effective {cycle_date}",
        "reconstructed_from": "Class_Airspace shapefile (polygons)",
        "filter": "Class B, C, D",
        "count": len(out),
        "airspaces": out,
    }

    seg_counts = {"circle": 0, "arc": 0, "line": 0}
    for a in out:
        for ring in a["rings"]:
            for s in ring:
                seg_counts[s["type"]] += 1
    print(f"Reconstructed {len(out)} airspaces: {seg_counts}")
    return result


def main():
    import argparse, tempfile, os

    parser = argparse.ArgumentParser(
        description="Reconstruct FAA class airspace boundaries as geometric primitives."
    )
    parser.add_argument(
        "--cycle",
        metavar="YYYY-MM-DD",
        help="NASR cycle effective date. Defaults to the most recent cycle.",
    )
    parser.add_argument(
        "--shapefile-dir",
        metavar="DIR",
        help="Use a local shapefile directory instead of downloading. "
             "Should contain Class_Airspace.shp/.dbf/.shx",
    )
    parser.add_argument(
        "--output",
        default="class_airspace_primitives.json",
        help="Output JSON file path (default: class_airspace_primitives.json)",
    )
    args = parser.parse_args()

    cycle_date = args.cycle or str(current_cycle_date())
    print(f"Cycle: {cycle_date}")

    if args.shapefile_dir:
        shp_dir = args.shapefile_dir
    else:
        tmp = tempfile.mkdtemp(prefix="nasr_")
        try:
            shp_dir = download_shapefile(cycle_date, tmp)
        except Exception as e:
            print(f"Download failed: {e}")
            print("You can manually download and unpack the shapefile, then use --shapefile-dir.")
            raise

    result = process_shapefile(shp_dir, cycle_date)

    with open(args.output, "w") as f:
        json.dump(result, f, separators=(",", ":"))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
