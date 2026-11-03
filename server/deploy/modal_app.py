"""Modal deployment: FastAPI + the built PWA. Data lives outside the containers.

Deploy from server/:  uv run modal deploy deploy/modal_app.py
Needs app/dist built first (cd app && npm run build).

Rows are in Postgres (Neon, pooled endpoint), files in Cloudflare R2, so containers
hold no state: Modal may run several and replace any of them. Writes to one claim
are serialised by a row lock in Postgres (tasks/t35-design.md), not in this process.
The old Volume `evidence-data` is no longer mounted; it stays as an archive.
"""

from pathlib import Path

import modal

SERVER_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = SERVER_DIR.parent / "app" / "dist"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("tzdata")
    .uv_sync(str(SERVER_DIR), extra_options="--no-dev")
    .env({"DATA_DIR": "/tmp/data", "FRONTEND_DIST": "/frontend"})
    .add_local_dir(DIST_DIR, "/frontend")
    # The whole package dir, not add_local_python_source: that one skips the JSON
    # templates, seed policies and SQL migrations.
    .add_local_dir(SERVER_DIR / "src" / "evidence_server", "/root/evidence_server", ignore=["**/__pycache__"])
)

app = modal.App("evidence-capture", image=image)


@app.function(
    secrets=[
        modal.Secret.from_name("evidence-admin"),  # ADMIN_PASSWORD, SESSION_SECRET; ADMIN_EMAIL optional (default "admin", T47)
        modal.Secret.from_name("evidence-seal"),  # SEAL_PRIVATE_KEY (Ed25519, base64 raw)
        modal.Secret.from_name("evidence-storage"),  # DATABASE_URL, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
        modal.Secret.from_name("evidence-meta"),  # LLAMA_API_KEY (Meta Model API); PHOTO_CHECK_LICENSE=on only with sample licences
        modal.Secret.from_name("evidence-fireworks"),  # FIREWORKS_API_KEY; MODEL_PROVIDER=fireworks or meta (T53)
    ],
    max_containers=10,
    min_containers=1,  # one warm server for judges and trial users (3 Nov); remove after judging
    scaledown_window=300,
    timeout=600,
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label="evidence-capture")
def web():
    from evidence_server.main import create_app

    return create_app()


@app.function(
    secrets=[modal.Secret.from_name(n) for n in ("evidence-admin", "evidence-seal", "evidence-storage", "evidence-meta",
                                                 "evidence-fireworks")],
    schedule=modal.Cron("*/2 * * * *"),
    timeout=300,
)
def sweep():
    from evidence_server.main import create_app

    print(create_app().state.store.sweep())
