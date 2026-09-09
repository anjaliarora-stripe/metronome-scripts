#!/usr/bin/env python3
"""Send usage events from events.json to Metronome.

Edit events.json, then run:

    python3 send_usage.py

Each run generates a new transaction_id unless you set one on the event,
so updated quantities are ingested as new usage (same ID is treated as a
duplicate and ignored for 34 days).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

BATCH_SIZE = 100
MAX_RETRIES = 5
ROOT = Path(__file__).resolve().parent
DEFAULT_EVENTS_FILE = ROOT / "events.json"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def stringify_properties(properties: dict) -> dict:
    out = {}
    for key, value in properties.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def load_events(path: Path, default_customer_id: str) -> list[dict]:
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        file_customer_id = ""
        raw_events = payload
    elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
        file_customer_id = str(payload.get("customer_id") or "")
        raw_events = payload["events"]
    else:
        raise SystemExit(
            f"{path} must be a JSON array of events, or an object with an 'events' array."
        )

    if not raw_events:
        raise SystemExit(f"{path} has no events to send.")

    prepared = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            raise SystemExit(f"Event at index {index} is not an object.")

        customer_id = (
            event.get("customer_id")
            or file_customer_id
            or default_customer_id
        )
        event_type = event.get("event_type")
        if not customer_id or str(customer_id).startswith("REPLACE_"):
            raise SystemExit(
                "Set customer_id in events.json, on each event, or as METRONOME_CUSTOMER_ID in .env."
            )
        if not event_type:
            raise SystemExit(f"Event at index {index} is missing event_type.")

        timestamp = event.get("timestamp") or now
        transaction_id = event.get("transaction_id") or (
            f"{event_type}_{customer_id}_{timestamp}_{uuid.uuid4().hex[:8]}"
        )
        if len(str(transaction_id)) > 128:
            raise SystemExit(
                f"Event at index {index} has a transaction_id longer than 128 characters."
            )

        prepared.append(
            {
                "transaction_id": str(transaction_id),
                "customer_id": str(customer_id),
                "event_type": str(event_type),
                "timestamp": str(timestamp),
                "properties": stringify_properties(event.get("properties") or {}),
            }
        )

    return prepared


def ingest_batch(base_url: str, token: str, batch: list[dict]) -> None:
    url = f"{base_url.rstrip('/')}/ingest"
    body = json.dumps(batch).encode("utf-8")
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                if response.status != 200:
                    raise SystemExit(
                        f"Ingest returned HTTP {response.status}: {response.read().decode()}"
                    )
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 or 500 <= exc.code <= 599:
                last_error = f"HTTP {exc.code}: {detail}"
                sleep_s = min(2 ** (attempt - 1), 16)
                print(f"Retryable error ({last_error}). Retrying in {sleep_s}s...")
                time.sleep(sleep_s)
                continue
            raise SystemExit(f"Ingest failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
            sleep_s = min(2 ** (attempt - 1), 16)
            print(f"Network error ({last_error}). Retrying in {sleep_s}s...")
            time.sleep(sleep_s)

    raise SystemExit(f"Ingest failed after {MAX_RETRIES} retries: {last_error}")


def chunks(items: list[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    parser = argparse.ArgumentParser(description="Send usage events to Metronome.")
    parser.add_argument(
        "--file",
        default=str(DEFAULT_EVENTS_FILE),
        help="Path to events JSON (default: events.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload without sending it",
    )
    args = parser.parse_args()

    load_env(ROOT / ".env")
    token = os.environ.get("METRONOME_API_TOKEN", "").strip()
    base_url = os.environ.get(
        "METRONOME_BASE_URL", "https://api.metronome.com/v1"
    ).strip()
    default_customer_id = os.environ.get("METRONOME_CUSTOMER_ID", "").strip()

    if not args.dry_run and not token:
        raise SystemExit("METRONOME_API_TOKEN is missing. Add it to .env.")

    events_path = Path(args.file)
    if not events_path.exists():
        raise SystemExit(f"Events file not found: {events_path}")

    events = load_events(events_path, default_customer_id)

    print(f"Prepared {len(events)} event(s) from {events_path}")
    for event in events:
        print(
            f"  {event['event_type']}  customer={event['customer_id']}  "
            f"txn={event['transaction_id']}  properties={event['properties']}"
        )

    if args.dry_run:
        print("\nDry run — nothing sent.")
        print(json.dumps(events, indent=2))
        return

    sent = 0
    for batch in chunks(events, BATCH_SIZE):
        ingest_batch(base_url, token, batch)
        sent += len(batch)
        print(f"Ingested batch of {len(batch)} event(s)")

    print(f"Done. Sent {sent} event(s) to {base_url}/ingest")


if __name__ == "__main__":
    sys.exit(main())
