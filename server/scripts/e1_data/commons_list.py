"""List free-licensed files in Wikimedia Commons categories (title, license, 1280px thumb URL)."""
import json, sys, httpx
UA = {"User-Agent": "EvidenceCaptureEval/0.1 (https://github.com/Julian-glitch-alt/evidence-capture; evaluation of an insurance claims prototype) python-httpx/0.27"}
FREE = ("cc0", "cc by", "cc-by", "public domain", "pd")
out = {}
for cat in sys.argv[2:]:
    gen = ({"generator": "search", "gsrsearch": cat[7:], "gsrnamespace": 6, "gsrlimit": 30} if cat.startswith("search:")
           else {"generator": "categorymembers", "gcmtitle": f"Category:{cat}", "gcmtype": "file", "gcmlimit": 40})
    r = httpx.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=60, params={
        "action": "query", "format": "json", **gen, "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
        "iiurlwidth": 1280})
    pages = (r.json().get("query") or {}).get("pages") or {}
    rows = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if ii.get("mime") not in ("image/jpeg", "image/png") or not lic.lower().startswith(FREE):
            continue
        rows.append({"title": p["title"], "license": lic, "thumb": ii.get("thumburl"), "page": ii.get("descriptionurl"),
                     "artist": (meta.get("Artist") or {}).get("value", "")[:120]})
    out[cat] = rows
    print(f"{cat}: {len(rows)} free files")
json.dump(out, open(sys.argv[1], "w"), indent=1)
