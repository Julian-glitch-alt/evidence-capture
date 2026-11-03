"""Upload latency vs concurrent customers, against a running server.

    ADMIN_PASSWORD=local-test uv run python scripts/load_test.py --customers 1,3 --photos 2

Reuses the smoke test's request sequence: admin login, one claim per customer
against DEMO-AUTO-1001, one capture session each, then one nonce per photo for
slots plate / vin_photo / odometer / damage_wide (cycled) with a small JPEG.

Setup (claims + sessions) is not timed. Then N threads upload at the same
moment through a threading.Barrier while each upload request is timed.
Prints one table row per N: customers, uploads, errors, p50 s, p95 s, max s, wall s.
"""

import argparse
import os
import threading
import time

import httpx

SLOTS = ("plate", "vin_photo", "odometer", "damage_wide")
JPEG = b"\xff\xd8\xff\xe0smoke-test-not-a-real-photo"
POLICY = "DEMO-AUTO-1001"


def percentile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    i = (len(sorted_xs) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def setup_customer(admin: httpx.Client, i: int) -> tuple[str, str]:
    r = admin.post("/api/admin/claims", json={"policy_number": POLICY})
    r.raise_for_status()
    body = r.json()
    token = body.get("token")
    if not token:
        token = admin.get(f"/api/admin/claims/{body['id']}").json()["token"]
    r = admin.post(f"/api/c/{token}/session")
    r.raise_for_status()
    return token, r.json()["session_id"]


def worker(
    barrier: threading.Barrier,
    base_url: str,
    tag: int,
    token: str,
    session_id: str,
    photos: int,
    durations: list[float],
    errors: list[int],
) -> None:
    http = httpx.Client(base_url=base_url, timeout=60)
    try:
        barrier.wait(timeout=60)
        for j in range(photos):
            slot = SLOTS[j % len(SLOTS)]
            try:
                r = http.post(
                    f"/api/c/{token}/nonce",
                    json={"session_id": session_id, "slot_id": slot},
                )
                nonce = r.json()["nonce"]
            except Exception:
                errors.append(1)
                continue
            data = JPEG + f"-{tag}-{j}".encode()
            t0 = time.perf_counter()
            try:
                r = http.post(
                    f"/api/c/{token}/items",
                    data={"step_id": slot, "session_id": session_id, "nonce": nonce},
                    files={"file": ("x.jpg", data, "image/jpeg")},
                )
            except Exception:
                errors.append(1)
                continue
            if r.status_code == 200:
                durations.append(time.perf_counter() - t0)
            else:
                errors.append(1)
    finally:
        http.close()


def run_level(base_url: str, admin: httpx.Client, n: int, photos: int) -> None:
    customers = [setup_customer(admin, i) for i in range(n)]
    barrier = threading.Barrier(n)
    durations: list[float] = []
    errors: list[int] = []
    threads = [
        threading.Thread(
            target=worker, args=(barrier, base_url, i, token, session_id, photos, durations, errors)
        )
        for i, (token, session_id) in enumerate(customers)
    ]
    wall0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0
    durations.sort()
    print(
        f"{n:<9} {len(durations):<7} {len(errors):<6} "
        f"{percentile(durations, 0.5):<7.3f} {percentile(durations, 0.95):<7.3f} "
        f"{(durations[-1] if durations else 0.0):<7.3f} {wall:<6.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--customers", default="1,5,10,20")
    parser.add_argument("--photos", type=int, default=3)
    args = parser.parse_args()
    admin = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60)
    r = admin.post("/api/admin/login", json={"email": os.environ.get("ADMIN_EMAIL", "admin"), "password": os.environ["ADMIN_PASSWORD"]})
    r.raise_for_status()
    print(f"{'customers':<9} {'uploads':<7} {'errors':<6} {'p50_s':<7} {'p95_s':<7} {'max_s':<7} {'wall_s':<6}")
    for n in [int(x) for x in args.customers.split(",") if x.strip()]:
        run_level(args.base_url.rstrip("/"), admin, n, args.photos)


if __name__ == "__main__":
    main()
