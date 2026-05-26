#!/usr/bin/env python3
"""
Parse FAA Coded Instrument Flight Procedures (CIFP) to extract controlled
airspace boundaries relevant to hobby aviation (floor <= 400 ft MSL).

Source: https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_YYMMDD.zip
Format: ARINC 424 fixed-width, 132 chars/line + CRLF
Records: Lines starting with "SUSAUC" (US controlled airspace boundaries)

Output: airspace.json with circle and polygon_arc geometries ready for
        direct use — no arc reconstruction required.
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

ARTCC_CLASS = {
    "PAB": "B",
    "PAC": "C",
    "PAD": "D",
    "PAE": "E",
    "PAF": "F",
}


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
    """Return feet MSL integer, or None on parse failure.

    GND / SFC -> 0
    UNL       -> 99999
    A07700    -> 7700   (strip leading alpha prefix)
    02500     -> 2500
    """
    s = s.strip()
    if not s or s in ("GND", "SFC"):
        return 0
    if s == "UNL":
        return 99999
    s = s.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
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
# ARINC 424 record parsing
# Column layout (0-based, both bounds inclusive):
#
#   0-5   SUSAUC        record type
#   6     K             customer code
#   7     region digit
#   8-12  airport ID    5 chars, e.g. "AKBIL"
#   13    space
#   14-16 ARTCC code    PAB/PAC/PAD/PAE
#   17-18 spaces
#   19    shell letter  A=innermost
#   20-24 sequence      00100, 00200, …
#   25-29 spaces
#   30-31 segment type  G / GE / R / CE
#
# Geometry (G/GE):
#   32-40  lat  NDDMMSSSS  (9 chars)
#   41-50  lon  WDDDMMSSSS (10 chars)
#
# Geometry (R):
#   32-40  arc end lat    (9 chars)
#   41-50  arc end lon    (10 chars)
#   51-59  center lat     (9 chars)
#   60-70  center lon     (11 chars incl. trailing space in file)
#   71-74  radius         tenths of NM  (4 chars)
#   75     direction      L or R
#
# Geometry (CE):
#   51-59  center lat     (9 chars)
#   60-70  center lon     (11 chars)
#   71-74  radius         (4 chars)
#
# First record of each shell (altitude / name):
#   81-85  floor altitude (5 chars)
#   86     separator
#   87-92  ceiling altitude (6 chars, may have A/B prefix)
#   93-122 airspace name
# ---------------------------------------------------------------------------

def parse_records(text):
    groups = {}  # (airport_id, artcc_code, shell_letter) -> [record, ...]

    for raw in text.splitlines():
        if not raw.startswith("SUSAUC"):
            continue
        line = raw.ljust(132)

        airport_id   = line[8:13].strip()
        artcc_code   = line[14:17].strip()
        shell_letter = line[19]
        seq_num      = line[20:25]
        seg_type     = line[30:32].rstrip()

        key = (airport_id, artcc_code, shell_letter)
        groups.setdefault(key, []).append({
            "line":     line,
            "seq":      seq_num,
            "seg_type": seg_type,
        })

    airspaces = []
    for (airport_id, artcc_code, shell_letter), records in groups.items():
        records.sort(key=lambda r: r["seq"])

        first     = records[0]["line"]
        floor_ft  = parse_altitude(first[81:86])
        ceil_ft   = parse_altitude(first[87:93])
        name      = first[93:123].strip()

        if floor_ft is None or floor_ft > 400:
            continue

        geometry = _build_geometry(records)
        if geometry is None:
            continue

        airspaces.append({
            "id":         f"{airport_id}_{artcc_code}_{shell_letter}",
            "name":       name,
            "airport":    airport_id,
            "icao":       airport_id[1:] if len(airport_id) > 1 else airport_id,
            "class":      ARTCC_CLASS.get(artcc_code, "E"),
            "floor_ft":   floor_ft,
            "ceiling_ft": ceil_ft,
            "geometry":   geometry,
        })

    return airspaces


def _build_geometry(records):
    for r in records:
        if r["seg_type"] == "CE":
            return _parse_ce(r["line"])

    segments = [s for r in records for s in [_parse_segment(r["line"], r["seg_type"])] if s]
    return {"type": "polygon_arc", "segments": segments} if segments else None


def _parse_ce(line):
    try:
        return {
            "type":       "circle",
            "center_lat": round(parse_lat(line[51:60]), 6),
            "center_lon": round(parse_lon(line[60:71]), 6),
            "radius_nm":  int(line[71:75]) / 10.0,
        }
    except (ValueError, IndexError):
        return None


def _parse_segment(line, seg_type):
    try:
        if seg_type in ("G", "GE"):
            return {
                "type": "point",
                "lat":  round(parse_lat(line[32:41]), 6),
                "lon":  round(parse_lon(line[41:51]), 6),
            }
        if seg_type == "R":
            direction = line[75] if len(line) > 75 and line[75] in ("L", "R") else "R"
            return {
                "type":       "arc",
                "end_lat":    round(parse_lat(line[32:41]), 6),
                "end_lon":    round(parse_lon(line[41:51]), 6),
                "center_lat": round(parse_lat(line[51:60]), 6),
                "center_lon": round(parse_lon(line[60:71]), 6),
                "radius_nm":  int(line[71:75]) / 10.0,
                "direction":  direction,
            }
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Cycle date helpers
# ---------------------------------------------------------------------------

def current_cycle_yymmdd():
    today = datetime.utcnow()
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
        "format":    "ARINC 424 (FAACIFP18)",
        "filter":    "Class B/C/D/E surface (floor_ft <= 400)",
        "count":     len(airspaces),
        "by_class":  counts,
        "generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "airspaces": airspaces,
    }

    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"Wrote {len(airspaces)} airspaces to {args.output}", file=sys.stderr)
    for cls in sorted(counts):
        print(f"  Class {cls}: {counts[cls]}", file=sys.stderr)


if __name__ == "__main__":
    main()
