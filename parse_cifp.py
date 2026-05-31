#!/usr/bin/env python3
"""
Parse FAA Coded Instrument Flight Procedures (CIFP) to extract all
Class B, C, and D controlled airspace boundaries.

Note: the FAA's CIFP UC subsection only contains Class B/C/D airspace.
Class E (and lower) is not in this dataset and would need to come from
another source such as the FAA's NASR subscription.

Source: https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_YYMMDD.zip
Format: ARINC 424-18 fixed-width, 132 chars/line + CRLF
Records: Lines starting with "SUSAUC" (US controlled airspace boundaries)

Output: airspace.json with circle and polygon_arc geometries ready for
        direct use, no arc reconstruction required.
"""

import argparse
import io
import json
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path


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


def _build_geometry(records):
    # A single CE record describes the whole airspace as a circle.
    for r in records:
        if r["bv"].startswith("CE"):
            return _parse_ce(r["line"])

    segments = []
    for r in records:
        seg = _parse_segment(r["line"], r["bv"])
        if seg is not None:
            segments.append(seg)
    return {"type": "polygon_arc", "segments": segments} if segments else None


def _parse_ce(line):
    try:
        return {
            "type":       "circle",
            "center_lat": round(parse_lat(line[51:60]), 6),
            "center_lon": round(parse_lon(line[60:70]), 6),
            "radius_nm":  int(line[70:74]) / 10.0,
        }
    except (ValueError, IndexError):
        return None


def _parse_segment(line, bv):
    """bv is the 2-char Boundary Via field from columns 30:32."""
    code = bv[0]
    try:
        if code in ("G", "H"):
            # Great circle or rhumb line to next point. We treat both as
            # straight segments; for class B/C/D in CONUS this is accurate
            # enough at the scale of these airspaces.
            return {
                "type": "point",
                "lat":  round(parse_lat(line[32:41]), 6),
                "lon":  round(parse_lon(line[41:51]), 6),
            }
        if code in ("R", "L"):
            return {
                "type":       "arc",
                "end_lat":    round(parse_lat(line[32:41]), 6),
                "end_lon":    round(parse_lon(line[41:51]), 6),
                "center_lat": round(parse_lat(line[51:60]), 6),
                "center_lon": round(parse_lon(line[60:70]), 6),
                "radius_nm":  int(line[70:74]) / 10.0,
                "direction":  "clockwise" if code == "R" else "counterclockwise",
            }
    except (ValueError, IndexError):
        pass
    return None


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
        description="Parse FAA CIFP airspace data into geometric primitives."
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
        "format":    "ARINC 424-18 (FAACIFP18)",
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


if __name__ == "__main__":
    main()
