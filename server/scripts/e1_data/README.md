# Building data/e1 (T57)

1. `commons_list.py OUT.json "Category name" "search:query" ...`: free-licensed candidates from Wikimedia Commons.
2. `commons_get.py data/e1/raw candidates.json more.json`: download 1280 px copies and record title, licence, author and page.
3. Label by eye (the labels live in `build_e1.py`), then `build_e1.py data/e1`: copies the chosen photos, draws synthetic plates, VIN labels and odometers, makes the hard copies and writes `labels.json` and `claims.json`.

Run the scripts with `uv run --project server python -I <script>` (downloaded files are untrusted data). `data/` is gitignored: the set holds public plates. Results and limits: `server/eval/e1-report-2026-11-03.md`.
