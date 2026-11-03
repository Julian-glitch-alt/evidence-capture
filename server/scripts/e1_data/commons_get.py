"""Download chosen candidates (1280px thumbnails) into an empty folder and record their sources."""
import json, re, sys, time
from pathlib import Path
import httpx
UA = {"User-Agent": "EvidenceCaptureEval/0.1 (https://github.com/Julian-glitch-alt/evidence-capture; evaluation of an insurance claims prototype) python-httpx/0.27"}
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
picks = {"License plates of California": ("plca", 20), "License plates of Texas": ("pltx", 12), "Odometers": ("odo", 16),
         "Hail damage": ("hail", 10), "Damaged automobiles": ("dmg", 22), "search:cracked windshield car": ("glass", 3),
         "search:car dent door": ("dent", 3), "search:bumper damage car accident": ("bump", 2),
         "search:2020 Honda Accord": ("accord", 8), "search:Toyota Camry 2019 front": ("camry", 6)}
cands = {**json.load(open(sys.argv[2])), **json.load(open(sys.argv[3]))}
sources = {}
for cat, (prefix, n) in picks.items():
    for i, row in enumerate(cands.get(cat, [])[:n]):
        name = f"{prefix}{i:02d}.jpg"
        try:
            r = httpx.get(row["thumb"], headers=UA, timeout=60, follow_redirects=True)
            r.raise_for_status()
            (out / name).write_bytes(r.content)
            sources[name] = {k: row[k] for k in ("title", "license", "page")} | {"artist": re.sub("<[^>]+>", "", row["artist"])}
        except Exception as e:
            print("skip", name, type(e).__name__)
        time.sleep(0.2)
json.dump(sources, open(out / "SOURCES.json", "w"), indent=1)
print(len(sources), "files")
