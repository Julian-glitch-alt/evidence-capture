"""Shared constants, upload validation and small helpers for the Store modules.

store.py is the facade; the domain modules (claims, capture, sealing,
reports, integration, retention, users/settings) are mixins of Store.
"""

import hashlib
import io
import json
import logging
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import psycopg.errors
from PIL import Image

from . import ai_summary, capture, claim_type, consistency, consistency_prompts, coverage, damage, damage_prompts, decision, desk, diary, estimate, evaluation, integration, lifecycle, loss_classify, loss_notice, meta_client, outline, photo_check, photo_prompts, policies, proposal, report, report_pdf, review, review_steps, seal, segment, signals, users, vocab
from .blobstore import BlobNotFound, BlobStore
from .db import Database
from .errors import CaptureError

# T45: the claimant header and button color, one per insurer (orgs.brand_color,
# migration 0016). NULL until a supervisor sets it; the claimant falls back to this.
DEFAULT_BRAND_COLOR = "#1673e6"
BRAND_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")

# T50: photos, video, the signature image and our report PDFs go 90 days after the decision.
RETENTION_DEFAULT_DAYS = 90
RETENTION_MIN_DAYS = 30
RETENTION_MAX_DAYS = 3650
RETENTION_SWEEP_LIMIT = 20

LINK_TTL = timedelta(days=7)
CAPTURE_STATUSES = {"opened", "capturing"}  # the claimant can still add evidence
REOPENABLE_STATUSES = CAPTURE_STATUSES | {"more_evidence"}  # waiting for the claimant (Send back included)

# Durable background work (track 3). The sweeper runs from a cron (deploy/README).
SWEEP_STALE_AFTER = timedelta(minutes=5)  # well past a model call's worst case (30 s queue + 30 s call)
ANALYSIS_MAX_ATTEMPTS = 3                 # the first run plus two sweeper re-runs
OUTBOX_MAX_ATTEMPTS = 8                   # about an hour of retries in all
OUTBOX_BACKOFF_BASE = timedelta(seconds=30)
OUTBOX_BACKOFF_CAP = timedelta(hours=1)
OUTBOX_LEASE = timedelta(seconds=60)      # longer than one POST (15 s timeout)


def _report_dir(claim_id: str, rnd: int) -> str:
    """Blob folder of one round's reports; round 1 keeps the original location."""
    return f"reports/{claim_id}" if rnd <= 1 else f"reports/{claim_id}/round-{rnd}"


def outbox_backoff(attempts: int) -> timedelta:
    """Wait before retry number `attempts` + 1: 30 s, 1 min, 2 min, 4 min, ... capped."""
    return min(OUTBOX_BACKOFF_BASE * (2 ** max(attempts - 1, 0)), OUTBOX_BACKOFF_CAP)

# The accuracy measured by the E1 set (scripts/e1_summary.py), shown on System health (T57).
ACCURACY_FILE = Path(__file__).parent / "reference" / "model_accuracy.json"
logger = logging.getLogger(__name__)

ALLOWED_MIME = {
    "photo": {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"},
    "video": {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"},
    "signature": {"image/png": "png"},
}
MAX_BYTES = {"photo": 15 * 1024 * 1024, "video": 80 * 1024 * 1024, "signature": 1024 * 1024}


@dataclass
class ItemFile:
    """Where a stored file lives: a presigned URL (S3/R2) or a local path."""

    mime: str
    url: str | None = None
    path: Path | None = None


def _new_claim_id(local_day: datetime) -> str:
    return f"CLM-{local_day:%Y%m%d}-{secrets.token_hex(3).upper()}"


@dataclass
class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _open_photo(data) -> "Image.Image | None":
    """Photo bytes -> an RGB Pillow image in its stored pixel orientation (as Muse Spark saw it), or None."""
    if not isinstance(data, bytes):
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image.convert("RGB")
    except Exception:
        return None


# --- upload validation: never trust the client-declared content type ---

_PILLOW_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
STATIC_SOURCE_THRESHOLD = 2.0  # mean abs pixel diff (0-255) below this is near-identical
SLOW_UPLOAD_SECONDS = 60.0


def _sniff_mime(data: bytes) -> str | None:
    """The mime implied by the magic bytes, or None when no known type matches."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    return None


def _validate_upload(data: bytes, declared_mime: str, kind: str) -> None:
    """Reject bytes that do not match the declared mime or do not decode.

    The declared mime must already be allowed for `kind` (checked by the
    caller); here the magic bytes must agree with it and, for images, Pillow
    must decode the file (`Image.open` + `load`) as that format.
    """
    sniffed = _sniff_mime(data)
    if sniffed is None or sniffed != declared_mime:
        raise CaptureError(
            f"The uploaded file does not match its declared type ({declared_mime})")
    if kind in ("photo", "signature"):
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:
            raise CaptureError("The uploaded image could not be decoded") from None
        if _PILLOW_FORMAT_MIME.get(image.format or "") != declared_mime:
            raise CaptureError(
                f"The uploaded file does not match its declared type ({declared_mime})")


def _mean_abs_diff(shot: bytes, frame: bytes) -> float | None:
    """Mean absolute pixel difference between the shot and one frame, or None
    when either side cannot be decoded. Both are downscaled to 64x64 grey."""
    try:
        a = Image.open(io.BytesIO(shot))
        a.load()
        b = Image.open(io.BytesIO(frame))
        b.load()
    except Exception:
        return None
    a = a.convert("L").resize((64, 64))
    b = b.convert("L").resize((64, 64))
    # Difference via histogram so no numpy is needed.
    from PIL import ImageChops

    diff = ImageChops.difference(a, b).histogram()
    total = sum(count * value for value, count in enumerate(diff))
    return total / (64 * 64)


def _liveness_assessment(kind: str, data: bytes, frame_before, frame_after) -> dict | None:
    """Liveness for a photo shot; None for kinds it does not apply to.

    Missing or near-identical frames flag a static source. This is a flag
    for the adjuster, never a block.
    """
    if kind != "photo":
        return None
    if not frame_before or not frame_after:
        return {
            "status": "flagged",
            "reason": "Liveness frames are missing, so a static source cannot be ruled out.",
            "frames_present": False,
            "mean_abs_diff_before": None,
            "mean_abs_diff_after": None,
        }
    diff_before = _mean_abs_diff(data, frame_before)
    diff_after = _mean_abs_diff(data, frame_after)
    if diff_before is None or diff_after is None:
        return {
            "status": "flagged",
            "reason": "Liveness frames could not be decoded, so a static source cannot be ruled out.",
            "frames_present": True,
            "mean_abs_diff_before": diff_before,
            "mean_abs_diff_after": diff_after,
        }
    if diff_before < STATIC_SOURCE_THRESHOLD and diff_after < STATIC_SOURCE_THRESHOLD:
        return {
            "status": "flagged",
            "reason": "Liveness frames are near-identical to the shot, which suggests a static source.",
            "frames_present": True,
            "mean_abs_diff_before": round(diff_before, 3),
            "mean_abs_diff_after": round(diff_after, 3),
        }
    return {
        "status": "ok",
        "reason": None,
        "frames_present": True,
        "mean_abs_diff_before": round(diff_before, 3),
        "mean_abs_diff_after": round(diff_after, 3),
    }


