# SAM 3.1 recorded answers (S0 probe, 28 Oct 2026)

Raw `output_text` from `sam-3.1` on the Meta Model API, recorded by `server/scripts/s0_probe.py`. The source photos are public Unsplash photos; `crops/` holds the crops the probe sent (Muse Spark's damage box plus a margin), so the `kind: "crop"` answers can be decoded and drawn over the image they describe. `kind: "full"` answers came from full photos that are not in the repo (only the text, for the format). `kind: "empty"` is a call that found nothing (empty string).

Format seen (documented in `docs/architecture.md`, S0 findings): `<0f>` (probably a frame index), then per instance `N<|box;x1=..;y1=..;x2=..;y2=..;w=W;h=H|><|mask;x=0;y=0;data=h,w,~...|>`. Box coordinates are in pixels of the image as SAM saw it (`w`, `h` = that image's size). The mask is box-sized and downscaled to at most 1024 px, in an undocumented text encoding: decoding it is part of track 6.
