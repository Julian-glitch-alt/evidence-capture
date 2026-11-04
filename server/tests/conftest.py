import json
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from pg import TEST_DATABASE_URL, reset

# evidence_server.main builds its module-level app only on first use (track 3);
# should anything touch it, it must reach the test database.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
# Tests never call Meta: a key in the shell must not reach create_app().
os.environ.pop("LLAMA_API_KEY", None)
os.environ.pop("PHOTO_CHECK_LICENSE", None)
# Track 3: callback hosts the tests' insurer uses (the app's own host is always allowed).
os.environ["CALLBACK_ALLOWED_HOSTS"] = "insurer.example,x.example"
# Track 5: tests run the expected-cost lane rule; production keeps proposal.DEFAULT_LANE_RULE until Julian decides.
os.environ["LANE_RULE"] = "cost"
os.environ.setdefault("SCRYPT_N", "16")  # T47: cheap password hashing in tests only

from evidence_server.blobstore import LocalBlobStore
from evidence_server.db import Database
from evidence_server.main import create_app
from evidence_server.consistency_prompts import STORY
from evidence_server.damage_prompts import DAMAGE
from evidence_server import photo_prompts
from evidence_server.photo_prompts import READS
from evidence_server.segment import MODEL as SAM_MODEL

try:  # T40: the AI summary prompt (until it exists the report treats the call as failed)
    from evidence_server.ai_summary import SUMMARY
except ImportError:
    SUMMARY = None

try:  # T60: the loss-type classifier prompt (until it exists nothing calls it)
    from evidence_server.loss_classify import PROMPT_HEAD as CLASSIFY
except ImportError:
    CLASSIFY = None

ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_EMAIL = "admin@example.com"
os.environ["ADMIN_EMAIL"] = ADMIN_EMAIL


def _image_bytes(fmt: str, color) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, fmt)
    return buf.getvalue()


# Real, decodable images: the server sniffs magic bytes and decodes with Pillow.
JPEG = _image_bytes("JPEG", (120, 30, 200))
PNG = _image_bytes("PNG", (20, 180, 60))
FRAME_BEFORE = _image_bytes("JPEG", (0, 0, 0))
FRAME_AFTER = _image_bytes("JPEG", (255, 255, 255))
MP4 = b"\x00\x00\x00\x18ftypmp42fake-video"


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


# What the fake model answers when a test sets nothing: a clean shot of DEMO-AUTO-1001's car.
GOOD_READS = {
    "plate": [{"is_plate": True, "text": "DEMO101", "state": "CA", "unsure": False, "problem": "none"}],
    "vin_photo": [
        {"is_vin": True, "vin": "1HGCV1F39LA526018", "unsure": False, "problem": "none"},
        {"is_vin": True, "characters": list("1HGCV1F39LA526018"), "unsure": False, "problem": "none"},
    ],
    "odometer": [{"is_odometer": True, "reading": 56947, "unit": "mi", "unsure": False, "problem": "none"}],
    "driver_license": [
        {"is_license": True, "name": "JORDAN AVERY", "state": "CA", "expires": "2029-05-01", "unsure": False, "problem": "none"}
    ],
    "damage_closeup": [{"is_vehicle": True, "damage_visible": True, "parts": ["Rear bumper"], "unsure": False, "problem": "none"}],
    "damage_wide": [{"is_vehicle": True, "damage_visible": True, "parts": ["Rear bumper"], "unsure": False, "problem": "none"}],
}


# The damage reading after submit (T20): one new, moderate dent in the rear bumper.
GOOD_DAMAGE = {
    "is_vehicle": True,
    "damages": [{"area": "Rear bumper", "damage_type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new",
                 "box": [200, 450, 800, 700], "observed": "Rear bumper cover pushed in"}],
    "unsure": False,
}
# The statement-vs-photos check (T21): backing into a bollard fits a dented rear bumper.
GOOD_STORY = {
    "impact_from_story": "rear",
    "impact_from_damage": "rear",
    "consistent": "yes",
    "reasons": [{"text": "Backing into a bollard fits the dented rear bumper.", "damages": [1]}],
}
# The AI summary of the report (T40): one sentence citing the damage and the statement.
GOOD_SUMMARY = {"sentences": [{"text": "The claimant backed into a bollard and the dented rear bumper matches that.", "refs": ["D1", "S"]}]}
# T60: what the classifier answers when a test sets nothing: a rock on the windshield is glass.
GOOD_CLASSIFY = {"loss_type": "glass", "confidence": "high", "reason": "A rock hit the windshield."}
DEFAULT_READS = GOOD_READS | {"damage": [GOOD_DAMAGE], "story": [GOOD_STORY], "summary": [GOOD_SUMMARY],
                              "classify": [GOOD_CLASSIFY]}


class FakeModel:
    """Stands in for the Meta transport. Knows the slot from the prompt; records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.answers: dict[str, list] = {}
        self.slot_of = {prompt: (slot, i) for slot, prompts in READS.items() for i, prompt in enumerate(prompts)}
        self.slot_of[DAMAGE] = ("damage", 0)
        for slot, prompt in getattr(photo_prompts, "RECHECKS", {}).items():  # T49: the second read is read 1
            self.slot_of[prompt] = (slot, 1)
        self.by_image: dict[bytes, object] = {}
        self.sam: dict[str, object] = {}  # track 6: SAM answers by concept; unset -> "" (found nothing)

    def set(self, slot: str, *answers) -> None:
        """One answer per read: a dict, a raw string, or an exception to raise."""
        self.answers[slot] = list(answers)

    def set_image(self, image: bytes, answer) -> None:
        """One answer for every call on these exact bytes; wins over set()."""
        self.by_image[image] = answer

    def set_sam(self, concept: str, answer) -> None:
        """SAM's raw output_text (or an exception) for every call with this concept."""
        self.sam[concept] = answer

    def __call__(self, model: str, prompt: str, image: bytes, mime: str, timeout: float) -> dict:
        if model == SAM_MODEL:  # the prompt is the concept; SAM answers plain text and reports no tokens
            self.calls.append({"slot": "sam", "read": 0, "model": model, "image": image, "mime": mime,
                               "timeout": timeout, "prompt": prompt})
            answer = self.sam.get(prompt, "")
            if isinstance(answer, Exception):
                raise answer
            return {"text": answer, "input_tokens": 0, "output_tokens": 0}
        # The story prompt carries the claim after a fixed head (T21).
        if prompt.startswith(STORY):
            slot, read = "story", 0
        elif CLASSIFY and prompt.startswith(CLASSIFY):  # T60
            slot, read = "classify", 0
        elif SUMMARY and prompt.startswith(SUMMARY):  # T42: a retry takes the next answer
            slot, read = "summary", sum(1 for c in self.calls if c["slot"] == "summary")
        else:
            slot, read = self.slot_of[prompt]
        self.calls.append({"slot": slot, "read": read, "model": model, "image": image, "mime": mime, "timeout": timeout,
                           "prompt": prompt})
        if image in self.by_image:
            answer = self.by_image[image]
        else:
            answers = self.answers.get(slot) or DEFAULT_READS[slot]
            answer = answers[min(read, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return {"text": text, "input_tokens": 1000, "output_tokens": 200}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(scope="session", autouse=True)
def _migrate_once():
    """Apply migrations once for the whole session; each test starts empty."""
    db = Database(TEST_DATABASE_URL)
    try:
        db.migrate()
    finally:
        db.close()


@pytest.fixture
def anon(tmp_path, clock):
    """A client with no adjuster session (claimant or attacker)."""
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        reset(admin)
    finally:
        admin.close()
    app = create_app(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "none",
        admin_password=ADMIN_PASSWORD,
        session_secret="test-secret",
        clock=clock,
        database_url=TEST_DATABASE_URL,
        blobstore=LocalBlobStore(tmp_path / "blobs"),
        model_transport=FakeModel(),
        check_license=False,
    )
    yield TestClient(app, base_url="https://testserver")
    app.state.store.db.close()


@pytest.fixture
def model(anon) -> FakeModel:
    """The fake Meta transport behind the app's MetaClient."""
    return anon.app.state.store.meta.transport


@pytest.fixture
def client(anon):
    """A client logged in as the adjuster."""
    r = anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return anon


class Phone:
    """The claimant's phone: opens a capture session, asks a nonce per shot, uploads."""

    def __init__(self, client, token):
        self.client, self.token = client, token
        r = client.post(f"/api/c/{token}/session")
        assert r.status_code == 200, r.text
        self.session = r.json()

    def nonce(self, slot_id):
        r = self.client.post(
            f"/api/c/{self.token}/nonce", json={"session_id": self.session["session_id"], "slot_id": slot_id}
        )
        assert r.status_code == 200, r.text
        return r.json()["nonce"]

    def upload(self, slot_id, data=JPEG, mime="image/jpeg", nonce=None, session_id=None,
               frames=True):
        files = {"file": ("x", data, mime)}
        if frames and mime.startswith("image/") and slot_id != "signature":
            # Liveness frames that differ from the shot, so a normal upload is not flagged.
            files["frame_before"] = ("frame_before.jpg", FRAME_BEFORE, "image/jpeg")
            files["frame_after"] = ("frame_after.jpg", FRAME_AFTER, "image/jpeg")
        return self.client.post(
            f"/api/c/{self.token}/items",
            data={
                "step_id": slot_id,
                "session_id": session_id or self.session["session_id"],
                "nonce": nonce if nonce is not None else self.nonce(slot_id),
                "client_meta": json.dumps({"camera": "back", "captured_at": "2026-10-28T15:00:00+00:00"}),
            },
            files=files,
        )
