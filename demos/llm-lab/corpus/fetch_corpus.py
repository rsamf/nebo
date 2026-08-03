"""Fetch 12 Wikipedia intro extracts (4 per category) as the demo corpus.

Run once to (re)generate the committed corpus:  python fetch_corpus.py
Text is CC BY-SA 4.0 — see the generated ATTRIBUTION.md.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent

TITLES = {
    "science": ["Photosynthesis", "Plate_tectonics", "Neutron_star", "CRISPR"],
    "history": ["Rosetta_Stone", "Silk_Road", "Printing_press", "Antikythera_mechanism"],
    "technology": ["Transistor", "Public-key_cryptography",
                   "Global_Positioning_System", "Lithium-ion_battery"],
}

API = "https://en.wikipedia.org/api/rest_v1/page/summary/"


def fetch(title: str) -> str:
    req = urllib.request.Request(API + title, headers={"User-Agent": "nebo-demo/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["extract"]


if __name__ == "__main__":
    attribution = ["# Corpus attribution", "",
                   "Intro extracts from the English Wikipedia (CC BY-SA 4.0):", ""]
    for category, titles in TITLES.items():
        for title in titles:
            text = fetch(title)
            path = OUT / f"{category}__{title}.txt"
            path.write_text(text)
            attribution.append(f"- [{title.replace('_', ' ')}](https://en.wikipedia.org/wiki/{title})")
            print(f"{title}  {len(text)} chars")
    (OUT / "ATTRIBUTION.md").write_text("\n".join(attribution) + "\n")
