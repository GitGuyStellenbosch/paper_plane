"""Assemble the website into _site/ for GitHub Pages (and local previews).

_site/
  index.html, places.json   copied from site/
  data/deals.json           copied from data/
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"

if OUT.exists():
    shutil.rmtree(OUT)
shutil.copytree(ROOT / "site", OUT)
(OUT / "data").mkdir()
shutil.copy2(ROOT / "data" / "deals.json", OUT / "data" / "deals.json")
print(f"Built {OUT}")
