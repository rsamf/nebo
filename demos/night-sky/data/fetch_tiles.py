"""Fetch real DSS2 (red) sky-survey cutouts via the CDS hips2fits service.

Run once to (re)generate the committed tiles:  python fetch_tiles.py
The tiles are committed so the pipeline demo runs fully offline.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent

# (name, ra_deg, dec_deg, fov_deg)
TILES = [
    ("pleiades", 56.75, 24.12, 1.2),
    ("orion_belt", 84.05, -1.20, 1.5),
    ("m67", 132.85, 11.81, 0.6),
    ("m13", 250.42, 36.46, 0.5),
    ("sagittarius", 271.00, -24.40, 1.0),
    ("cygnus", 308.00, 41.00, 1.0),
]

BASE = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"


def fetch(name: str, ra: float, dec: float, fov: float) -> None:
    query = urllib.parse.urlencode({
        "hips": "CDS/P/DSS2/red",
        "ra": ra,
        "dec": dec,
        "fov": fov,
        "width": 512,
        "height": 512,
        "projection": "TAN",
        "format": "png",
    })
    req = urllib.request.Request(f"{BASE}?{query}", headers={"User-Agent": "nebo-demo/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    (OUT / f"{name}.png").write_bytes(data)
    print(f"{name}.png  {len(data) // 1024} KB")


if __name__ == "__main__":
    for tile in TILES:
        fetch(*tile)
