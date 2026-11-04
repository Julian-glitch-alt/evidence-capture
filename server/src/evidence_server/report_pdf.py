"""Render the verified evidence report as a PDF (T40, tasks/t40-design.md).

fpdf2 with the core Helvetica font, which is latin-1 only: every string goes
through report.latin1, wrapped text through multi_cell, and very long words
are broken up so rendering never raises. One filled badge per page (the lane);
status otherwise reads as icon + word in colour, never a fill.
"""

import base64
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from PIL import Image, ImageDraw, ImageFont

from . import ai_summary as ai_summary_mod
from . import estimate as estimate_lib
from . import outline as outline_lib
from . import report as report_lib

FOOTER = ("Recommendation from code; the adjuster decides. "
          "Prices are public ballpark figures, not an estimate.")
_RESULT_WORDS = {"pass": "PASS", "fail": "FAIL", "unclear": "REVIEW", "value": "INFO"}
_RESULT_ICONS = {"pass": "[ok]", "fail": "[!]", "unclear": "[?]", "value": ""}
_LONG_WORD = re.compile(r"\S{31,}")
_MARKS = {"conflict": "[!]", "review": "[?]"}
_MAX_PHOTO_H = 90  # mm; a photo taller than this shares a row badly
YELLOW_RGB = (255, 210, 63)
TEAL_SOFT = (227, 241, 239)
BAND_H = 30.0
FINDING_THUMB_W = 45.0
STATUS_COL_W = 22.0
MAP_MAX_W = 110.0
SECTION_MIN = 40.0

PASS_RGB = (31, 95, 158)
REVIEW_RGB = (168, 100, 10)
CONFLICT_RGB = (196, 68, 26)
INK_RGB = (21, 33, 44)
GREY_RGB = (100, 116, 139)
SEAL_RGB = (14, 107, 103)
_LANE_FILL = {
    "siu_referral": (196, 68, 26),
    "adjuster_review": (168, 100, 10),
    "fast_track": (31, 95, 158),
    "standard": (125, 138, 151),
}

ZONES = {
    "Front bumper": (30, 52, 22, 66),
    "Hood": (56, 40, 64, 90),
    "Windshield": (124, 42, 40, 86),
    "Roof": (168, 40, 52, 90),
    "Rear window": (222, 42, 18, 86),
    "Trunk or tailgate": (242, 40, 26, 90),
    "Rear bumper": (268, 52, 22, 66),
    "Front left": (56, 24, 64, 14),
    "Left side": (124, 24, 114, 14),
    "Rear left": (242, 24, 48, 14),
    "Front right": (56, 132, 64, 14),
    "Right side": (124, 132, 114, 14),
    "Rear right": (242, 132, 48, 14),
}
WHEEL_CENTRES = [(88, 24), (88, 146), (254, 24), (254, 146)]
WHEEL_R = 9
MAP_W, MAP_H = 320, 170


class _Report(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 8, _text(f"{FOOTER} Page {self.page_no()} of {{nb}}"), align="C",
                    new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def render(report: dict, images: dict[str, bytes] | None = None, ai_summary=None) -> bytes:
    """Letter size, 2+ pages: summary band and evidence first, cost and checks after."""
    images = images or {}
    pdf = _Report(format="Letter")
    pdf.alias_nb_pages("{nb}")
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    _head(pdf, report)
    _claim_form(pdf, report)
    _band(pdf, report)
    _evidence_line(pdf, report)
    _seal_line(pdf, report)
    _summary(pdf, report)
    _ai_summary(pdf, report, ai_summary)
    _findings(pdf, report, images)
    _damage_map(pdf, report)
    _damage_list(pdf, report)
    _cost(pdf, report)
    _ai_notes(pdf, report)
    _checks(pdf, report)
    _photos(pdf, report, images)
    _review_block(pdf, report)
    _seal(pdf, report)
    return bytes(pdf.output())


def _text(value) -> str:
    return report_lib.latin1("" if value is None else str(value))


def _wrapped(value) -> str:
    """Wrapped text with very long words broken up so multi_cell never raises."""

    def _break(match: re.Match) -> str:
        word = match.group(0)
        return " ".join(word[i:i + 30] for i in range(0, len(word), 30))

    return _LONG_WORD.sub(_break, _text(value))


def _h(pdf: _Report, title: str) -> None:
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(0, 7, _text(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)


def _p(pdf: _Report, text: str, size: int = 11, grey: bool = False) -> None:
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*GREY_RGB) if grey else pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(0, 5.5, _wrapped(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _colored(pdf: _Report, text: str, rgb: tuple, size: int = 11, bold: bool = False) -> None:
    pdf.set_font("Helvetica", "B" if bold else "", size)
    pdf.set_text_color(*rgb)
    pdf.multi_cell(0, 5.5, _wrapped(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _need(pdf: _Report, h: float = SECTION_MIN) -> None:
    """Never leave a heading alone at the bottom: break if less than h remains."""
    if pdf.get_y() + h > pdf.h - 20:
        pdf.add_page()


def _short(value) -> str:
    """First 8 ... last 4 of a hash, latin-1 safe."""
    s = _text(value)
    if len(s) < 12:
        return s
    return f"{s[:8]}...{s[-4:]}"


def _mono(pdf: _Report, text: str, size: int = 9) -> None:
    pdf.set_font("Courier", "", size)
    pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(0, 5, _wrapped(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _sealed_local(report: dict) -> str:
    """The seal time in the claim's local time, formatted like generated_local."""
    raw = (report.get("seal") or {}).get("sealed_at") or report.get("generated_at") or ""
    tzname = report.get("tz")
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return _text(raw)
    try:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("UTC"))
        if tzname:
            moment = moment.astimezone(ZoneInfo(str(tzname)))
    except Exception:
        pass
    hour12 = moment.hour % 12 or 12
    suffix = moment.tzname() or ""
    return _text(f"{moment:%b} {moment.day}, {moment.year} {hour12}:{moment:%M}"
                 f" {'AM' if moment.hour < 12 else 'PM'}" + (f" {suffix}" if suffix else ""))


def _one_line(value, limit: int = 64) -> str:
    s = " ".join(_text(value).split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "..."


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _damage_words(dtype) -> str:
    return str(dtype or "-").lower().replace("_", " ").capitalize()


def _head(pdf: _Report, report: dict) -> None:
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*INK_RGB)
    title = ("TEST CLAIM · " if report.get("test_mode") else "") + "Verified Evidence Report"
    pdf.multi_cell(0, 9, _text(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    claim = report.get("insurer_claim_number") or "-"
    _p(pdf, f"{report.get('org')}  ·  Claim {claim}  ·  Case {report.get('case_id')}")
    _p(pdf, _text(f"Generated {report.get('generated_local')}"), grey=True)
    decision = report.get("decision") or {}
    if decision.get("by"):
        when = (decision.get("at") or "")[:10]
        _p(pdf, f"Reviewed by {decision.get('by')}" + (f"  ·  {when}" if when else ""))
    pdf.ln(2)


def _claim_cell(pdf: _Report, x: float, y: float, w: float, h: float, label: str, value: str,
                  mono: bool = False) -> None:
    pdf.set_draw_color(213, 220, 227)
    pdf.set_line_width(0.2)
    pdf.rect(x, y, w, h, style="D")
    pdf.set_xy(x + 2, y + 1.5)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*GREY_RGB)
    pdf.multi_cell(w - 4, 3.5, _text(label).upper(), new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_x(x + 2)
    pdf.set_font("Courier" if mono else "Helvetica", "", 10 if not mono else 9)
    pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(w - 4, 4.5, _wrapped(value), new_x=XPos.LEFT, new_y=YPos.NEXT)


def _claim_row(pdf: _Report, cells: list) -> None:
    """One grid row of (label, value, span, mono); height fits the tallest value."""
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    col = usable / 4
    pdf.set_font("Helvetica", "", 10)
    need = 0
    for _, value, span, mono in cells:
        w = col * span - 4
        pdf.set_font("Courier" if mono else "Helvetica", "", 9 if mono else 10)
        lines = 1
        for paragraph in _wrapped(value).split("\n"):
            width = pdf.get_string_width(paragraph) if paragraph else 0
            lines = max(lines, max(1, int(width // max(1, w) + 1)))
        need = max(need, lines)
    row_h = max(13.0, 6.0 + need * 4.5)
    if pdf.get_y() + row_h > pdf.h - 20:
        pdf.add_page()
    y = pdf.get_y()
    x = pdf.l_margin
    for label, value, span, mono in cells:
        w = col * span
        _claim_cell(pdf, x, y, w, row_h, label, value, mono)
        x += w
    pdf.set_xy(pdf.l_margin, y + row_h)


def _policy_cell(report: dict) -> str:
    """The Policy cell names where the policy came from when it rode along."""
    number = report.get("policy_number") or "-"
    source = report.get("policy_source")
    if source == "trial":
        return f"{number} (trial policy)"
    if source == "insurer":
        return f"{number} (sent by the insurer)"
    return number


def _claim_form(pdf: _Report, report: dict) -> None:
    vehicle = report.get("vehicle") or {}
    car = " ".join(str(vehicle.get(k) or "") for k in ("year", "make", "model")).strip() or "-"
    loss = report.get("loss") or {}
    vin = vehicle.get("vin") or "-"
    plate = vehicle.get("plate") or ""
    vin_value = vin if not plate else f"{vin} . plate {plate}"
    _claim_row(pdf, [
        ("Claim number", _text(report.get("insurer_claim_number") or "-"), 1, False),
        ("Verification case", _text(report.get("case_id") or "-"), 1, False),
        ("Policy", _text(_policy_cell(report)), 1, False),
        ("Exposure", f"Vehicle damage, {_text(loss.get('type') or '-')}", 1, False),
    ])
    _claim_row(pdf, [
        ("Insured", _text(report.get("insured") or "-"), 1, False),
        ("Vehicle", _text(car), 1, False),
        ("VIN", vin_value, 2, True),
    ])
    _claim_row(pdf, [
        ("Date of loss", _text(loss.get("date") or "-"), 1, False),
        ("Cause", _text(loss.get("type") or "-"), 1, False),
        ("Statement", _text(str(loss.get("description") or "-")), 2, False),
    ])
    claim_type = report.get("claim_type") or {}
    if isinstance(claim_type, dict) and claim_type.get("label"):
        _claim_row(pdf, [("Claim type", _text(claim_type.get("label")), 4, False)])
    for line in report.get("to_verify") or []:  # reported by the claimant, not verified by us
        _claim_row(pdf, [("To verify", _text(line), 4, False)])
    pdf.ln(2)


def _physical(report: dict) -> list[dict]:
    """The damage list counted once per physical damage; older report data has only per-photo damages."""
    if "damage_list" in report:
        return report.get("damage_list") or []
    return report.get("damages") or []


def _is_excluded(d: dict, dropped: set) -> bool:
    """A physical damage is excluded when the adjuster excluded any photo listing of it."""
    return bool(set(d.get("keys") or [d.get("key")]) & dropped)


def _excluded_keys(report: dict) -> set:
    damages = ((report.get("review") or {}).get("damages")) or {}
    return {k for k, v in damages.items() if (v or {}).get("verdict") == "excluded"}


def _band_tile(pdf: _Report, x: float, y: float, w: float, h: float, label: str,
                 big: str, sub: str, fill=None, big_rgb=None) -> None:
    if fill is not None:
        pdf.set_fill_color(*fill)
        pdf.rect(x, y, w, h, style="F")
    pdf.set_xy(x + 3, y + 2.5)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(255, 255, 255) if fill is not None else pdf.set_text_color(*GREY_RGB)
    pdf.multi_cell(w - 6, 3.5, _text(label).upper(), new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_x(x + 3)
    pdf.set_font("Helvetica", "B", 15)
    if big_rgb is not None:
        pdf.set_text_color(*big_rgb)
    elif fill is not None:
        pdf.set_text_color(255, 255, 255)
    else:
        pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(w - 6, 7, _text(big), new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_x(x + 3)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(255, 255, 255) if fill is not None else pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(w - 6, 4.5, _wrapped(sub), new_x=XPos.LEFT, new_y=YPos.NEXT)


def _band(pdf: _Report, report: dict) -> None:
    rec = report.get("recommendation") or {}
    lane = rec.get("lane") or "standard"
    fill = _LANE_FILL.get(lane, _LANE_FILL["standard"])
    flags = report.get("flags") or []
    conflicts = sum(1 for f in flags if f.get("level") == "conflict")
    reviews = sum(1 for f in flags if f.get("level") == "review")
    checks = report.get("checks") or []
    passed = sum(1 for c in checks if c.get("result") == "pass")
    cost = report.get("cost") or {}
    likely, full = cost.get("likely"), cost.get("full")
    if cost.get("status") != "none" and likely and full:
        big_cost = f"{_money(likely[0])}-{_money(likely[1])}"
        reserve = cost.get("suggested_reserve")
        sub_cost = (f"Suggested initial reserve {_money(reserve)}"
                    if type(reserve) is int else "Suggested initial reserve -")
    else:
        big_cost, sub_cost = "-", "No rough range"
    lane_label = _text(rec.get("label") or lane).upper()
    next_step = _one_line(rec.get("next_step") or "")
    big_findings = str(len(flags))
    sub_findings = f"{_plural(conflicts, 'conflict', 'conflicts')} . {_plural(reviews, 'to review', 'to review')}"
    big_checks = f"{passed}/{len(checks)}"
    if pdf.get_y() + BAND_H > pdf.h - 20:
        pdf.add_page()
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    parts = [1.35, 1.0, 1.0, 1.2]
    total = sum(parts)
    widths = [usable * p / total for p in parts]
    y = pdf.get_y()
    x = pdf.l_margin
    tiles = [
        ("Recommended lane", lane_label, next_step, fill, None),
        ("Findings", big_findings, sub_findings, None, None),
        ("Checks", big_checks, "passed", None, None),
        ("Likely repair cost", big_cost, sub_cost, None, None),
    ]
    for (label, big, sub, tile_fill, big_rgb), w in zip(tiles, widths):
        _band_tile(pdf, x, y, w, BAND_H, label, big, sub, tile_fill, big_rgb)
        x += w
    pdf.set_draw_color(*INK_RGB)
    pdf.set_line_width(0.3)
    pdf.rect(pdf.l_margin, y, usable, BAND_H, style="D")
    x = pdf.l_margin
    for w in widths[:-1]:
        x += w
        pdf.line(x, y, x, y + BAND_H)
    pdf.set_xy(pdf.l_margin, y + BAND_H)


def evidence_text(report: dict) -> str | None:
    """Track 5: "Evidence strength: moderate (vehicle identity, odometer)." or None for an older report."""
    ev = report.get("evidence")
    if not ev or not ev.get("band"):
        return None
    causes = ", ".join(ev.get("causes") or [])
    text = f"Evidence strength: {ev['band']}" + (f" ({causes})." if causes else ".")
    if ev.get("missing"):
        text += f" Not checked: {', '.join(ev['missing'])}."
    return text


def _evidence_line(pdf: _Report, report: dict) -> None:
    text = evidence_text(report)
    if text is None:
        return
    pdf.ln(1.5)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(0, 4.5, _wrapped(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*GREY_RGB)
    pdf.multi_cell(0, 3.5, _text((report.get("evidence") or {}).get("note") or ""), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)


def seal_text(report: dict) -> str:
    """The seal strip text. It names a VIN and plate match only when report.vehicle_matches_policy
    (verified policy, both photo checks pass); with no policy on file it says so."""
    seal = report.get("seal") or {}
    when = _sealed_local(report)
    items = seal.get("items")
    try:
        count = f"{int(items)} items captured in the capture flow"
    except (TypeError, ValueError):
        count = f"{items} items captured in the capture flow"
    parts = [f"SEALED {when}", count]
    if report_lib.vehicle_matches_policy(report):
        parts.append("VIN and plate photos match the policy")
    elif report.get("policy_verified") is not True:
        parts.append("no verified policy on file")
    manifest = seal.get("manifest_sha256") or ""
    if len(_text(manifest)) >= 12:
        parts.append(f"manifest {_short(manifest)}")
    return " . ".join(p for p in parts if p and p != "None")


def _seal_line(pdf: _Report, report: dict) -> None:
    seal = report.get("seal") or {}
    if not seal:
        return
    text = seal_text(report)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font("Helvetica", "", 9)
    lines = max(1, int(pdf.get_string_width(_wrapped(text)) // max(1, usable - 6) + 1))
    strip_h = max(8.0, lines * 5 + 3)
    y = pdf.get_y()
    if y + strip_h > pdf.h - 20:
        pdf.add_page()
        y = pdf.get_y()
    pdf.set_fill_color(*TEAL_SOFT)
    pdf.set_draw_color(*INK_RGB)
    pdf.set_line_width(0.2)
    pdf.rect(pdf.l_margin, y, usable, strip_h, style="DF")
    pdf.set_xy(pdf.l_margin + 3, y + 1.5)
    pdf.set_text_color(*SEAL_RGB)
    head_text, marker, tail_hash = _wrapped(text).partition("manifest ")
    pdf.set_font("Helvetica", "", 9)
    pdf.write(5, head_text + marker)
    if tail_hash:
        pdf.set_font("Courier", "", 9)
        pdf.write(5, tail_hash)
    pdf.set_xy(pdf.l_margin, y + strip_h)
    pdf.ln(2)


def _summary(pdf: _Report, report: dict) -> None:
    _need(pdf)
    _h(pdf, "Summary")
    try:
        _p(pdf, report_lib.summary(report))
    except Exception:
        _p(pdf, "-")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*GREY_RGB)
    pdf.multi_cell(0, 5, _text("Built by code from the checks and findings below; no new facts."),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _ai_summary(pdf: _Report, report: dict, ai_summary=None) -> None:
    ai = ai_summary if ai_summary is not None else report.get("ai_summary")
    if not isinstance(ai, dict) or ai.get("status") != "verified":
        return
    _need(pdf)
    _h(pdf, "AI summary")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*GREY_RGB)
    pdf.multi_cell(0, 5, _text(f"AI summary  ·  {ai_summary_mod.CAPTION}  ·  verify in the photos."),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    left = pdf.get_x()
    for sentence in ai.get("sentences") or []:
        refs = " ".join(f"[{r}]" for r in sentence.get("refs") or [])
        line = f"{sentence.get('text')} {refs}".strip()
        y0 = pdf.get_y()
        pdf.set_fill_color(227, 241, 239)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(*INK_RGB)
        pdf.multi_cell(0, 5.5, _wrapped(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y1 = pdf.get_y()
        x = pdf.l_margin
        pdf.set_fill_color(14, 107, 103)
        pdf.rect(x - 3, y0, 1.2, max(4, y1 - y0), style="F")
        pdf.set_x(left)
    void = left  # keep the anchor readable; no-op
    pdf.ln(2)


def _expand_box(box: list, frac: float = 0.4) -> list:
    x1, y1, x2, y2 = (float(v) for v in box)
    w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)
    return [max(0.0, x1 - frac * w), max(0.0, y1 - frac * h),
            min(1000.0, x2 + frac * w), min(1000.0, y2 + frac * h)]


def _tag_font():
    """Bold tag font around 7 pt; truetype when available, else the default."""
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "ArialBd.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, 14)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=14)
    except Exception:
        pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


_TAG_FONT = None


def _paint_box(draw: ImageDraw.ImageDraw, size: tuple, box: list, number) -> None:
    global _TAG_FONT
    width, height = size
    x1, y1, x2, y2 = box[0] / 1000 * width, box[1] / 1000 * height, \
        box[2] / 1000 * width, box[3] / 1000 * height
    draw.rectangle([x1, y1, x2, y2], outline=YELLOW_RGB, width=3)
    tag = str(number)
    if _TAG_FONT is None:
        _TAG_FONT = _tag_font()
    try:
        bbox = draw.textbbox((0, 0), tag, font=_TAG_FONT)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = 6 * len(tag), 10
    pad_x, pad_y = 4, 3
    tx, ty = x1, max(0, y1 - (th + pad_y * 2))
    draw.rectangle([tx, ty, tx + tw + pad_x * 2, ty + th + pad_y * 2], fill=YELLOW_RGB)
    try:
        draw.text((tx + pad_x, ty + pad_y), tag, fill=(21, 33, 44), font=_TAG_FONT)
    except Exception:
        draw.text((tx + pad_x, ty + pad_y), tag, fill=(21, 33, 44))


def _finding_thumb_data(photo_id: str | None, box: list | None, number, images: dict):
    """Cropped JPEG around the evidence box (box expanded 40% each side, clamped)."""
    if not photo_id or (images or {}).get(photo_id) is None:
        return None
    try:
        img = Image.open(io.BytesIO(images[photo_id]))
        img.load()
    except Exception:
        return None
    if img.mode != "RGB":
        img = img.convert("RGB")
    width, height = img.size
    if box:
        crop = _expand_box(box)
        cx1, cy1, cx2, cy2 = crop[0] / 1000 * width, crop[1] / 1000 * height, \
            crop[2] / 1000 * width, crop[3] / 1000 * height
        if cx2 - cx1 < 4 or cy2 - cy1 < 4:
            return None
        img = img.crop((cx1, cy1, cx2, cy2))
        rel = [(box[0] - crop[0]) / max(1e-6, crop[2] - crop[0]) * 1000,
               (box[1] - crop[1]) / max(1e-6, crop[3] - crop[1]) * 1000,
               (box[2] - crop[0]) / max(1e-6, crop[2] - crop[0]) * 1000,
               (box[3] - crop[1]) / max(1e-6, crop[3] - crop[1]) * 1000]
        _paint_box(ImageDraw.Draw(img), img.size, rel, number)
    out = io.BytesIO()
    try:
        img.save(out, "JPEG")
    except Exception:
        return None
    return out.getvalue(), img.size[0], img.size[1]


def _status_box(pdf: _Report, x: float, y: float, level: str) -> None:
    rgb = CONFLICT_RGB if level == "conflict" else REVIEW_RGB if level == "review" else GREY_RGB
    word = "CONFLICT" if level == "conflict" else "REVIEW" if level == "review" else "NOTE"
    mark = _MARKS.get(level, "[?]")
    pdf.set_draw_color(*rgb)
    pdf.set_line_width(0.3)
    pdf.rect(x, y, STATUS_COL_W, 8, style="D")
    pdf.set_xy(x + 1, y + 1.5)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*rgb)
    pdf.multi_cell(STATUS_COL_W - 2, 5, f"{mark} {word}", new_x=XPos.LEFT, new_y=YPos.NEXT)


def _finding_match(flag: dict, damages: list, photos: list):
    for label in flag.get("evidence") or []:
        match = next((d for d in damages if d.get("label") == label and d.get("box")), None)
        if match is not None:
            return match.get("item_id"), match.get("box"), match.get("number")
        photo = next((p for p in photos if p.get("label") == label), None)
        if photo is not None:
            return photo.get("item_id"), None, "?"
    return None, None, None


def _findings(pdf: _Report, report: dict, images: dict) -> None:
    _need(pdf)
    _h(pdf, "Findings")
    flags = report.get("flags") or []
    review_flags = ((report.get("review") or {}).get("flags")) or {}
    damages = report.get("damages") or []
    photos = report.get("photos") or []
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    mid_w = usable - STATUS_COL_W - FINDING_THUMB_W - 6
    for flag in flags:
        key = flag.get("key") or f"{flag.get('source')}:{flag.get('reason_code')}"
        level = flag.get("level")
        photo_id, box, number = _finding_match(flag, damages, photos)
        thumb = _finding_thumb_data(photo_id, box, number, images)
        thumb_h = FINDING_THUMB_W * thumb[2] / thumb[1] if thumb else 0
        thumb_h = min(thumb_h, 45.0) if thumb else 0
        if pdf.get_y() + max(14.0, thumb_h + 4) > pdf.h - 20:
            pdf.add_page()
        y0 = pdf.get_y()
        x0 = pdf.l_margin
        _status_box(pdf, x0, y0, level)
        mid_x = x0 + STATUS_COL_W + 3
        pdf.set_xy(mid_x, y0)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*INK_RGB)
        pdf.multi_cell(mid_w, 5, _wrapped(flag.get("reason")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        evidence = ", ".join(flag.get("evidence") or [])
        if evidence:
            pdf.set_x(mid_x)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*GREY_RGB)
            pdf.multi_cell(mid_w, 4.5, _wrapped(evidence), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        verdict = (review_flags.get(key) or {}).get("verdict")
        if verdict == "confirmed":
            pdf.set_x(mid_x)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*GREY_RGB)
            pdf.multi_cell(mid_w, 4.5, _wrapped(f"Confirmed by {(review_flags[key] or {}).get('by') or '-'}"),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif verdict == "false_alarm":
            pdf.set_x(mid_x)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*GREY_RGB)
            pdf.multi_cell(mid_w, 4.5, _wrapped(f"False alarm: {(review_flags[key] or {}).get('reason') or ''}"),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y1 = pdf.get_y()
        if thumb:
            thumb_x = pdf.l_margin + usable - FINDING_THUMB_W
            pdf.image(io.BytesIO(thumb[0]), x=thumb_x, y=y0, w=FINDING_THUMB_W, h=thumb_h)
            y1 = max(y1, y0 + thumb_h)
        bottom = max(y1, y0 + 10) + 2
        pdf.set_draw_color(213, 220, 227)
        pdf.set_line_width(0.2)
        pdf.line(x0, bottom, x0 + usable, bottom)
        pdf.set_xy(x0, bottom + 2)
    for note in report.get("notes") or []:
        evidence = ", ".join(note.get("evidence") or [])
        line = f"[i] {_text(note.get('reason'))}"
        if evidence:
            line += f" ({_text(evidence)})"
        _p(pdf, line, grey=True)
    if not flags and not (report.get("notes") or []):
        _p(pdf, "No findings.")
    pdf.ln(2)


def _zone_state(area: str, seen_areas: set, declared: set) -> str:
    if area in seen_areas and area in declared:
        return "both"
    if area in seen_areas:
        return "seen"
    if area in declared:
        return "declared"
    return "none"


def _legend_row(pdf: _Report, x: float, y: float, usable: float) -> float:
    items = [("Declared and seen", (229, 238, 248), PASS_RGB, False),
             ("Seen, not declared", (251, 240, 220), REVIEW_RGB, True),
             ("Declared, not seen", (255, 255, 255), CONFLICT_RGB, False)]
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*GREY_RGB)
    cx = x
    top = y
    for label, fill, edge, dashed in items:
        tag = _wrapped(label)
        tw = pdf.get_string_width(tag) + 12
        if cx + tw > x + usable and cx > x:
            cx = x
            top += 6
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*edge)
        pdf.set_line_width(0.3)
        if dashed:
            try:
                pdf.set_dash_pattern(1.5, 1)
            except Exception:
                pass
        pdf.rect(cx, top + 1, 6, 4, style="DF")
        try:
            pdf.set_dash_pattern()
        except Exception:
            pass
        pdf.set_xy(cx + 8, top)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY_RGB)
        pdf.multi_cell(tw - 8, 5, tag, new_x=XPos.LEFT, new_y=YPos.NEXT)
        cx += tw
    return top + 6


def _damage_map(pdf: _Report, report: dict) -> None:
    _need(pdf)
    _h(pdf, "Damage map")
    damages = _physical(report)
    dropped = _excluded_keys(report)
    declared = set(report.get("declared_areas") or [])
    seen: dict[str, int] = {}
    for i, d in enumerate(damages, 1):
        if _is_excluded(d, dropped):
            continue
        if d.get("area") not in seen:
            seen[d.get("area")] = i
    seen_areas = set(seen)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    map_w = min(MAP_MAX_W, usable)
    scale = map_w / MAP_W
    map_h = MAP_H * scale
    if pdf.get_y() + map_h + 14 > pdf.h - 20:
        pdf.add_page()
    x0 = pdf.l_margin + (usable - map_w) / 2
    y0 = pdf.get_y()
    pdf.set_fill_color(243, 246, 248)
    pdf.rect(x0, y0, map_w, map_h, style="F")
    for area, (zx, zy, zw, zh) in ZONES.items():
        state = _zone_state(area, seen_areas, declared)
        x, y, w, h = x0 + zx * scale, y0 + zy * scale, zw * scale, zh * scale
        if state == "both":
            pdf.set_fill_color(229, 238, 248)
            pdf.set_draw_color(*PASS_RGB)
            try:
                pdf.set_dash_pattern()
            except Exception:
                pass
            pdf.rect(x, y, w, h, style="DF")
        elif state == "seen":
            pdf.set_fill_color(251, 240, 220)
            pdf.set_draw_color(*REVIEW_RGB)
            try:
                pdf.set_dash_pattern(1.5, 1)
            except Exception:
                pass
            pdf.rect(x, y, w, h, style="DF")
            try:
                pdf.set_dash_pattern()
            except Exception:
                pass
        elif state == "declared":
            pdf.set_fill_color(255, 255, 255)
            pdf.set_draw_color(*CONFLICT_RGB)
            try:
                pdf.set_dash_pattern()
            except Exception:
                pass
            pdf.rect(x, y, w, h, style="DF")
        else:
            pdf.set_fill_color(255, 255, 255)
            pdf.set_draw_color(213, 220, 227)
            pdf.rect(x, y, w, h, style="D")
        if area in seen_areas:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*INK_RGB)
            pdf.set_xy(x + w / 2 - 4, y + h / 2 - 3)
            pdf.cell(8, 6, str(seen[area]), align="C",
                     new_x=XPos.LEFT, new_y=YPos.NEXT)
    wheels_seen = "Wheels or tires" in seen_areas
    for cx, cy in WHEEL_CENTRES:
        x, y = x0 + (cx - WHEEL_R) * scale, y0 + (cy - WHEEL_R) * scale
        d = 2 * WHEEL_R * scale
        if wheels_seen and "Wheels or tires" in declared:
            pdf.set_fill_color(229, 238, 248)
            pdf.set_draw_color(*PASS_RGB)
            pdf.ellipse(x, y, d, d, style="DF")
        elif wheels_seen:
            pdf.set_fill_color(251, 240, 220)
            pdf.set_draw_color(*REVIEW_RGB)
            pdf.ellipse(x, y, d, d, style="DF")
        else:
            pdf.set_draw_color(213, 220, 227)
            pdf.ellipse(x, y, d, d, style="D")
    try:
        pdf.set_dash_pattern()
    except Exception:
        pass
    pdf.set_draw_color(0, 0, 0)
    end_y = _legend_row(pdf, pdf.l_margin, y0 + map_h + 2, usable)
    pdf.set_xy(pdf.l_margin, end_y + 1)
    pdf.ln(1)


def _damage_list(pdf: _Report, report: dict) -> None:
    _need(pdf)
    _h(pdf, "Damage")
    damages = _physical(report)
    dropped = _excluded_keys(report)
    reasons = ((report.get("review") or {}).get("damages")) or {}
    if not damages:
        _p(pdf, "No damage recorded.")
    for i, d in enumerate(damages, 1):
        words = _damage_words(d.get("damage_type"))
        seen_in = ", ".join(d.get("labels") or [d.get("label")])
        line = f"{i}. {seen_in}: {d.get('area')}, {words}, {d.get('severity')}, {d.get('age')}"
        if _is_excluded(d, dropped):
            key = next((k for k in d.get("keys") or [d.get("key")] if k in dropped), None)
            line += f" -- excluded: {(reasons.get(key) or {}).get('reason') or ''}"
            _p(pdf, line, grey=True)
        else:
            if d.get("age") == "old":
                line += " -- possibly old"
            _p(pdf, line)
    pdf.ln(2)


def _money(value) -> str:
    return f"${value:,}" if type(value) is int else "-"


def _cost(pdf: _Report, report: dict) -> None:
    if pdf.page_no() == 1 and pdf.get_y() > pdf.h / 2:
        pdf.add_page()
    _need(pdf, 60.0)
    _h(pdf, "Cost")
    cost = report.get("cost") or {}
    likely, full = cost.get("likely"), cost.get("full")
    if cost.get("status") == "none" or not likely or not full:
        _p(pdf, "No rough range: the damage photos could not be priced.")
    else:
        lo, hi = full
        span = max(1, hi - lo)
        usable = pdf.w - pdf.l_margin - pdf.r_margin
        if pdf.get_y() + 34 > pdf.h - 20:
            pdf.add_page()
        x0 = pdf.l_margin
        y = pdf.get_y() + 10
        pdf.set_fill_color(213, 220, 227)
        pdf.rect(x0, y - 2, usable, 4, style="F")
        lx = x0 + (likely[0] - lo) / span * usable
        lw = max(2, (likely[1] - likely[0]) / span * usable)
        pdf.set_fill_color(*SEAL_RGB)
        pdf.rect(lx, y - 2, lw, 4, style="F")
        mid = cost.get("midpoint")
        if type(mid) is int and lo <= mid <= hi:
            mx = x0 + (mid - lo) / span * usable
            pdf.set_draw_color(*INK_RGB)
            pdf.set_line_width(0.6)
            pdf.line(mx, y - 5, mx, y + 5)
            pdf.set_line_width(0.2)
            pdf.set_xy(mx - 30, y - 10)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*INK_RGB)
            pdf.multi_cell(60, 4.5, _text(f"midpoint {_money(mid)}"), align="C",
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_xy(x0, y + 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK_RGB)
        pdf.multi_cell(30, 4.5, _text(_money(lo)), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_xy(lx - 15, y + 4)
        pdf.multi_cell(30, 4.5, _text(_money(likely[0])), align="C",
                       new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_xy(lx + lw - 15, y + 4)
        pdf.multi_cell(30, 4.5, _text(_money(likely[1])), align="C",
                       new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_xy(x0 + usable - 30, y + 4)
        pdf.multi_cell(30, 4.5, _text(_money(hi)), align="R",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_xy(pdf.l_margin, y + 10)
    _p(pdf, _text(cost.get("basis")), size=10, grey=True)
    deductible = cost.get("deductible")
    _p(pdf, f"Deductible: {_money(deductible)}" if type(deductible) is int else "Deductible: unknown")
    if type(cost.get("suggested_reserve")) is int:
        _p(pdf, f"Suggested initial reserve {_money(cost['suggested_reserve'])}"
                " (a suggestion; the adjuster sets the reserve)")
    driver = _main_driver(report)
    if driver is not None:
        _p(pdf, f"Main cost driver: damage {driver} (public ballpark figures; the appraiser writes the estimate).",
           size=10, grey=True)
    pdf.ln(2)


def _main_driver(report: dict) -> str | None:
    """The priced damage with the highest high end, or None."""
    dropped = _excluded_keys(report)
    best, best_high = None, None
    for i, d in enumerate(_physical(report), 1):
        if _is_excluded(d, dropped) or d.get("age") == "old":
            continue
        cell = (estimate_lib.TABLE.get(d.get("damage_type")) or {}).get(d.get("severity"))
        if not cell:
            continue
        if best_high is None or cell[1] > best_high:
            words = _damage_words(d.get("damage_type"))
            best, best_high = f"{i}, {d.get('area')}, {words}, {d.get('severity')}", cell[1]
    return best


def _ai_notes(pdf: _Report, report: dict) -> None:
    observed = [(i, d) for i, d in enumerate(_physical(report), 1) if (d.get("observed") or "").strip()]
    stories = report.get("story_reasons") or []
    if not observed and not stories:
        return
    _need(pdf)
    _h(pdf, "What the AI saw")
    for i, d in observed:
        _p(pdf, f"Damage {i}: {_text(d.get('observed'))} -- verify in {', '.join(d.get('labels') or [d.get('label')])}", size=11)
    for entry in stories:
        labels = ", ".join(entry.get("evidence") or [])
        line = f"Statement: {_text(entry.get('text'))}"
        if labels:
            line += f" -- verify in {labels}"
        _p(pdf, line, size=11)
    _p(pdf, "Values are kept only when they match the fixed lists; code, not the model, sets every flag.", size=10, grey=True)
    pdf.ln(2)


def _check_result_text(result: str) -> str:
    word = _RESULT_WORDS.get(result, str(result or "").upper())
    icon = _RESULT_ICONS.get(result, "[?]")
    return f"{icon} {word}".strip() if word != "INFO" else "INFO"


def _check_rgb(result: str) -> tuple:
    if result == "pass":
        return PASS_RGB
    if result == "fail":
        return CONFLICT_RGB
    if result == "value":
        return GREY_RGB
    return REVIEW_RGB


def _check_row_h(pdf: _Report, widths: list, check: dict) -> float:
    cols = [_wrapped(check.get("title")), _check_result_text(check.get("result")),
            _wrapped(check.get("reason"))]
    fonts = [("Helvetica", "", 10), ("Helvetica", "B", 9), ("Helvetica", "", 10)]
    need = 1
    for (fam, style, size), text, w in zip(fonts, cols, widths):
        pdf.set_font(fam, style, size)
        lines = 1
        for paragraph in text.split("\n"):
            width = pdf.get_string_width(paragraph) if paragraph else 0
            lines = max(lines, max(1, int(width // max(1, w - 2) + 1)))
        need = max(need, lines)
    return max(7.0, need * 5 + 2)


def _checks(pdf: _Report, report: dict) -> None:
    _need(pdf)
    _h(pdf, "Checks")
    groups = [("COVERAGE", "coverage."), ("PHOTOS", "photo."),
              ("STATEMENT VS PHOTOS", "consistency.")]
    by_group: dict[str, list] = {name: [] for name, _ in groups}
    for check in report.get("checks") or []:
        check_id = check.get("check_id") or ""
        for name, prefix in groups:
            if check_id.startswith(prefix):
                by_group[name].append(check)
                break
        else:
            by_group.setdefault("OTHER", []).append(check)
    order = {"fail": 0, "unclear": 1, "pass": 2, "value": 3}
    names = [n for n, _ in groups] + (["OTHER"] if "OTHER" in by_group else [])
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    widths = [usable * 0.44, usable * 0.20, usable * 0.36]
    headers = ["Check", "Result", "Detail"]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*GREY_RGB)
    y0 = pdf.get_y()
    bottom = y0
    x = pdf.l_margin
    for head, w in zip(headers, widths):
        pdf.set_xy(x, y0)
        pdf.multi_cell(w, 5, _text(head).upper(), new_x=XPos.LEFT, new_y=YPos.NEXT)
        bottom = max(bottom, pdf.get_y())
        x += w
    pdf.set_y(bottom)
    y = bottom
    pdf.set_draw_color(213, 220, 227)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, y, pdf.l_margin + usable, y)
    pdf.set_y(y + 1)
    for name in names:
        rows = sorted(by_group.get(name) or [], key=lambda c: order.get(c.get("result"), 4))
        if not rows:
            continue
        if pdf.get_y() + 12 > pdf.h - 20:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*INK_RGB)
        pdf.multi_cell(0, 5, _text(name).upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        for check in rows:
            row_h = _check_row_h(pdf, widths, check)
            if pdf.get_y() + row_h + 2 > pdf.h - 20:
                pdf.add_page()
            y0 = pdf.get_y()
            x = pdf.l_margin
            result = check.get("result")
            pdf.set_xy(x, y0)
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*INK_RGB)
            pdf.multi_cell(widths[0], 5, _wrapped(check.get("title")),
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
            pdf.set_xy(x + widths[0], y0)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*_check_rgb(result))
            pdf.multi_cell(widths[1], 5, _text(_check_result_text(result)),
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
            pdf.set_xy(x + widths[0] + widths[1], y0)
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*GREY_RGB)
            pdf.multi_cell(widths[2], 5, _wrapped(check.get("reason")),
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
            bottom = max(pdf.get_y(), y0 + row_h)
            pdf.set_draw_color(213, 220, 227)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, bottom, pdf.l_margin + usable, bottom)
            pdf.set_xy(pdf.l_margin, bottom + 1)
    pdf.ln(2)


def _drawn(photo: dict, data: bytes, damages: list[dict]) -> tuple | None:
    """The photo with its damage boxes and numbers drawn on, as (JPEG bytes, w, h)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    if img.mode != "RGB":
        img = img.convert("RGB")
    for d in damages:  # SAM 3.1 outlines first, so Muse Spark's numbered boxes stay on top (track 6)
        if d.get("item_id") == photo.get("item_id"):
            _paint_outline(img, (d.get("outline") or {}).get("regions") or [])
    numbered = [(d.get("box"), d.get("number")) for d in damages
                if d.get("item_id") == photo.get("item_id") and d.get("box")]
    if numbered:
        draw = ImageDraw.Draw(img)
        for box, number in numbered:
            _paint_box(draw, img.size, box, number if number is not None else "?")
    out = io.BytesIO()
    try:
        img.save(out, "JPEG")
    except Exception:
        return None
    return out.getvalue(), img.size[0], img.size[1]


def _paint_outline(img: Image.Image, regions: list[dict]) -> None:
    """SAM regions (box in 0-1000 of the photo) onto the photo: the decoded outline overlay,
    or the region's box in the outline colour when the mask did not decode."""
    width, height = img.size
    for region in regions:
        box = region.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        x1, y1 = round(box[0] / 1000 * width), round(box[1] / 1000 * height)
        x2, y2 = round(box[2] / 1000 * width), round(box[3] / 1000 * height)
        if x2 - x1 < 1 or y2 - y1 < 1:
            continue
        try:
            overlay = Image.open(io.BytesIO(base64.b64decode(region["overlay"]))) if region.get("overlay") else None
        except Exception:
            overlay = None
        if overlay is not None:
            overlay = overlay.convert("RGBA").resize((x2 - x1, y2 - y1), Image.NEAREST)
            img.paste(overlay, (x1, y1), overlay)
        else:
            ImageDraw.Draw(img).rectangle([x1, y1, x2, y2], outline=outline_lib.OUTLINE_RGB, width=2)


def _outline_ran(outline) -> bool:
    """Whether SAM ran for one damage: a stored outline whose status is not "not_run".
    Report damages predate the status field, so without one a yes/no answer counts as ran."""
    if not isinstance(outline, dict):
        return False
    if outline.get("status") is not None:
        return outline.get("status") != "not_run"
    return outline.get("sam_agrees") in ("yes", "no")


def _box_caption(photo: dict, damages: list[dict]) -> str:
    """The box legend for one photo: with SAM only where SAM ran on this photo."""
    for d in damages or []:
        if d.get("item_id") != photo.get("item_id") or not d.get("box"):
            continue
        if _outline_ran(d.get("outline")):
            return "Yellow box: Muse Spark; blue outline: SAM 3.1"
    return "Yellow box: Muse Spark"


def _outline_notes(photo: dict, damages: list[dict]) -> list[str]:
    """Per damage on this photo: what the outline model added, as subordinate notes."""
    notes = []
    for d in damages:
        if d.get("item_id") != photo.get("item_id") or not d.get("box"):
            continue
        if not _outline_ran(d.get("outline")):
            continue
        o = d.get("outline") or {}
        tag = f"#{d.get('number')}"
        if o.get("sam_agrees") == "no":
            notes.append(f"{tag}: {outline_lib.NOTE_DISAGREE}")
        elif o.get("sam_agrees") != "yes":
            notes.append(f"{tag}: {outline_lib.NOTE_MISSING}")
        area = o.get("area") or {}
        if o.get("sam_agrees") == "yes" and area.get("photo_pct") is not None:
            text = f"{tag}: outline covers about {area['photo_pct']:g}% of the photo"
            if area.get("panel_pct") is not None:
                text += f", {area['panel_pct']:g}% of the {area.get('panel') or 'panel'}"
            notes.append(text + ("" if area.get("basis") == "mask" else " (box estimate)"))
    return notes


def _photos(pdf: _Report, report: dict, images: dict[str, bytes]) -> None:
    _need(pdf)
    _h(pdf, "Photos")
    damages = report.get("damages") or []
    ready = []
    for photo in report.get("photos") or []:
        data = (images or {}).get(photo.get("item_id"))
        if data is None:
            continue
        drawn = _drawn(photo, data, damages)
        if drawn is not None:
            ready.append((photo, *drawn))
    if not ready:
        _p(pdf, "No photos available.", grey=True)
        pdf.ln(2)
        return
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    gap = 6
    cell = (usable - gap) / 2
    col, row_top, row_h = 0, pdf.get_y(), 0.0
    for photo, data, width, height in ready:
        height_mm = min(cell * height / width, _MAX_PHOTO_H)
        width_mm = height_mm * width / height
        if col == 0:
            row_top = pdf.get_y()
            if row_top + height_mm + 12 > pdf.h - 20:
                pdf.add_page()
                row_top = pdf.get_y()
        x = pdf.l_margin + col * (cell + gap)
        pdf.image(io.BytesIO(data), x=x, y=row_top, w=width_mm, h=height_mm)
        pdf.set_xy(x, row_top + height_mm + 1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY_RGB)
        flags = photo.get("flags") or []
        flag_notes = []
        if "static_source" in flags:
            flag_notes.append("static source (liveness frames missing or near-identical)")
        if "slow_upload" in flags:
            flag_notes.append("slow upload (more than 60 s after the capture code)")
        caption = (f"{photo.get('label')} — flagged: {'; '.join(flag_notes)}"
                   if flag_notes else photo.get("label"))
        pdf.multi_cell(cell, 4.5, _wrapped(caption), new_x=XPos.LEFT, new_y=YPos.NEXT)
        if any(d.get("item_id") == photo.get("item_id") and d.get("box") for d in damages):
            pdf.set_x(x)
            pdf.set_font("Helvetica", "", 8)
            pdf.multi_cell(cell, 4, _text(_box_caption(photo, damages)),
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
            for note in _outline_notes(photo, damages):
                pdf.set_x(x)
                pdf.multi_cell(cell, 4, _wrapped(note), new_x=XPos.LEFT, new_y=YPos.NEXT)
        digest = photo.get("sha256") or photo.get("hash")
        if digest:
            pdf.set_x(x)
            pdf.set_font("Courier", "", 8)
            pdf.set_text_color(*GREY_RGB)
            pdf.multi_cell(cell, 4, _text(f"sha256 {_short(digest)}"),
                           new_x=XPos.LEFT, new_y=YPos.NEXT)
        row_h = max(row_h, height_mm + 8, pdf.get_y() - row_top + 2)
        col += 1
        if col == 2:
            pdf.set_y(row_top + row_h + gap)
            col, row_h = 0, 0.0
    if col == 1:
        pdf.set_y(row_top + row_h + gap)
    pdf.ln(2)


def _review_block(pdf: _Report, report: dict) -> None:
    state = report.get("review") or {}
    notes = state.get("notes") or []
    decision = report.get("decision")
    if not notes and not decision:
        return
    _need(pdf)
    _h(pdf, "Adjuster review")
    for entry in notes:
        line = f"Note: {_text(entry.get('text'))} -- {(entry.get('by') or '-')}"
        _p(pdf, line)
    if decision:
        _p(pdf, f"Decision by {decision.get('by')}: {decision.get('label') or decision.get('action')}. "
                f"Lane: {decision.get('lane_after') or '-'}.")
        if decision.get("reason"):
            _p(pdf, f"Reason: {_text(decision.get('reason'))}")
    pdf.ln(2)


def _seal(pdf: _Report, report: dict) -> None:
    _need(pdf)
    _h(pdf, "Evidence seal")
    seal = report.get("seal") or {}
    chain = report.get("chain") or {}
    if not seal:
        _p(pdf, "Not sealed.")
        return
    _p(pdf, f"Sealed {_sealed_local(report)}  ·  Key {_text(seal.get('key_id'))}")
    pdf.set_font("Courier", "", 7)
    pdf.set_text_color(*INK_RGB)
    pdf.multi_cell(0, 4.5, _text(f"Manifest SHA-256 {seal.get('manifest_sha256') or ''}"),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", "", 9)
    pdf.set_text_color(*INK_RGB)
    head = chain.get("head")
    items = seal.get("items")
    tail = f"  ·  Event-log head {_short(head)}" if head else ""
    pdf.multi_cell(0, 5, _wrapped(f"Items sealed: {items}{tail}"),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _p(pdf, "Captured in the capture flow and sealed. Anyone can verify the sealed package at /verify, "
            "or offline with tools/verify_package.py.", grey=True)
