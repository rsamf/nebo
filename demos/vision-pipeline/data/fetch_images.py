"""Fetch the demo's image set from Wikimedia Commons.

Run once to (re)generate the committed dataset:  python fetch_images.py
Each candidate file is looked up via the Commons API; only files under a
permissive license (CC0 / CC BY / CC BY-SA / public domain) are kept, and
ATTRIBUTION.md is generated from the API's author/license metadata.
Downloads are 640px thumbnails via Special:FilePath, so the committed set
stays small.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent
API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "nebo-demo/1.0 (https://github.com/graphbookai/nebo)"}

# Curated object-rich Commons files: streets, animals, rooms, vehicles,
# people, food. Existence and license are validated at fetch time; entries
# that fail lookup or carry a restrictive license are skipped with a note.
CANDIDATES = [
    # Verified keepers (previous fetch runs)
    "New_york_times_square-terabass.jpg",
    "Cat_November_2010-1a.jpg",
    "Felis_catus-cat_on_snow.jpg",
    "Golde33443.jpg",
    "Labrador_on_Quantock_(2175262184).jpg",
    "Elephant_near_ndutu.jpg",
    "Zebras_Serengeti.JPG",
    "Cow_female_black_white.jpg",
    "Sheep,_Stodmarsh_6.jpg",
    "Brown_bear_(Ursus_arctos_arctos)_running.jpg",
    "Bicycles_Amsterdam.jpg",
    "Amsterdam_-_Bicycles_-_1058.jpg",
    "London_Bus_route_9.jpg",
    "Kitchen_interior_design.jpg",
    "Fruit_Stall_in_Barcelona_Market.jpg",
    "Pizza_Margherita_stu_spivack.jpg",
    "Surfer_above_the_wave.jpg",
    "Traffic_lights_at_night.jpg",
    # Commons-search finds (license-checked at fetch time like all entries)
    "Crosswalk_of_Market_at_Third,_San_Francisco.jpg",
    "City_bus_in_Vlora_(PV917).jpg",
    "Living_Room_von_Egidius_Knops_in_Hamburg-Neuallermöhe_(4).jpg",
    "2021-07-20_02_Maine_Marine_Patrol_Boat_at_Winter_Harbor_ME_USA.jpg",
    "043_St._Gallen,_Switzerland_-_sidewalk_cafe.jpg",
    "Jeseník_train_station_2025.01.jpg",
    # Long-stable article-lead classics
    "Cat03.jpg",
    "YellowLabradorLooking_new.jpg",
    "African_Bush_Elephant.jpg",
    "Good_Food_Display_-_NCI_Visuals_Online.jpg",
    "Eq_it-na_pizza-margherita_sep2005_sml.jpg",
    "NCI_Visuals_Food_Hamburger.jpg",
    "Trabant601S.jpg",
    "Roger_Federer_2009_Australian_Open.jpg",
    "Rush_hour_at_Shinjuku_02.JPG",
    "Wildebeest_Mara.jpg",
    "Tomatoes-cherry.jpg",
    "Baseball_swing.jpg",
]

_OK_LICENSE = re.compile(r"(cc0|cc[ -]by(?:[ -]sa)?|public domain|pd)", re.I)


def lookup(title: str) -> dict | None:
    """Return {license, author} for a Commons file, or None if unusable."""
    query = urllib.parse.urlencode({
        "action": "query",
        "titles": f"File:{urllib.parse.unquote(title)}",
        "prop": "imageinfo",
        "iiprop": "extmetadata",
        "format": "json",
    })
    req = urllib.request.Request(f"{API}?{query}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        pages = json.loads(resp.read())["query"]["pages"]
    page = next(iter(pages.values()))
    info = (page.get("imageinfo") or [{}])[0].get("extmetadata")
    if not info:
        return None
    license_short = (info.get("LicenseShortName") or {}).get("value", "")
    if not _OK_LICENSE.search(license_short):
        return None
    author = re.sub(r"<[^>]+>", "", (info.get("Artist") or {}).get("value", "unknown")).strip()
    return {"license": license_short, "author": author or "unknown"}


def fetch(title: str, index: int) -> str | None:
    meta = lookup(title)
    if meta is None:
        print(f"skip {title} (missing or restrictive license)")
        return None
    url = (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        f"{title}?width=640"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    name = f"{index:03d}.jpg"
    (OUT / name).write_bytes(data)
    print(f"{name}  {len(data) // 1024} KB  {meta['license']}  <- {title}")
    return (
        f"| {name} | [{urllib.parse.unquote(title)}]"
        f"(https://commons.wikimedia.org/wiki/File:{title}) "
        f"| {meta['author']} | {meta['license']} |"
    )


if __name__ == "__main__":
    rows = [
        "# Image attribution",
        "",
        "All images from Wikimedia Commons, downscaled to 640px. These files",
        "are third-party content and are **not** covered by this repository's",
        "MIT license; each remains under the license listed below.",
        "",
        "| file | source | author | license |",
        "| --- | --- | --- | --- |",
    ]
    kept = 0
    for title in CANDIDATES:
        time.sleep(0.6)  # stay under the Commons API rate limit
        try:
            row = fetch(title, kept)
        except Exception as exc:  # noqa: BLE001 - per-file resilience
            print(f"skip {title} ({exc})")
            continue
        if row:
            rows.append(row)
            kept += 1
    (OUT / "ATTRIBUTION.md").write_text("\n".join(rows) + "\n")
    print(f"\nkept {kept}/{len(CANDIDATES)} images")
