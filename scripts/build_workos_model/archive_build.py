#!/usr/bin/env python3
"""Archive everything a build_workos_model.py run created, using its state file.

    python3 scripts/build_workos_model/archive_build.py --state-file scripts/build_workos_model/state-v1.json
    python3 scripts/build_workos_model/archive_build.py --state-file scripts/build_workos_model/state-v1.json --confirm

Without --confirm this only prints what would be archived. Objects are archived in
dependency order: contracts, customers, packages, rate card, products, billable
metrics. Archiving a contract cancels its draft invoices; finalized invoices are
kept unless --void-finalized is passed. Archived IDs are recorded in the state
file, so a re-run skips them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def post(root: str, token: str, path: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"{root}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request):
            return
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"POST {path} failed with HTTP {exc.code}: {exc.read().decode()}") from exc


def plan(state: dict, void_finalized: bool) -> list[tuple[str, str, str, dict]]:
    """Return (state section, key, endpoint, payload) in dependency order."""
    steps = []
    customers = state.get("customers", {})
    for key, contract_id in state.get("contracts", {}).items():
        steps.append(("contracts", key, "/v1/contracts/archive", {
            "customer_id": customers[key], "contract_id": contract_id, "void_invoices": void_finalized,
        }))
    for key, customer_id in customers.items():
        steps.append(("customers", key, "/v1/customers/archive", {"id": customer_id}))
    for key, package_id in state.get("packages", {}).items():
        steps.append(("packages", key, "/v1/packages/archive", {"package_id": package_id}))
    if state.get("rate_card", {}).get("id"):
        steps.append(("rate_card", "id", "/v1/contract-pricing/rate-cards/archive", {"id": state["rate_card"]["id"]}))
    for key, product_id in state.get("products", {}).items():
        steps.append(("products", key, "/v1/contract-pricing/products/archive", {"product_id": product_id}))
    for key, metric_id in state.get("billable_metrics", {}).items():
        steps.append(("billable_metrics", key, "/v1/billable-metrics/archive", {"id": metric_id}))
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive a WorkOS model build from its state file.")
    parser.add_argument("--state-file", required=True, help="State file written by build_workos_model.py")
    parser.add_argument("--confirm", action="store_true", help="Actually archive (default is a dry run)")
    parser.add_argument("--void-finalized", action="store_true", help="Also void finalized invoices on contracts")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text())
    archived = state.setdefault("archived", {})

    load_env(REPO_ROOT / ".env")
    token = os.environ.get("METRONOME_API_TOKEN", "").strip()
    root = os.environ.get("METRONOME_BASE_URL", "https://api.metronome.com/v1").strip().rstrip("/").removesuffix("/v1")
    if args.confirm and not token:
        raise SystemExit("METRONOME_API_TOKEN is missing. Add it to .env.")

    for section, key, endpoint, payload in plan(state, args.void_finalized):
        marker = f"{section}.{key}"
        object_id = payload.get("contract_id") or payload.get("package_id") or payload.get("product_id") or payload["id"]
        if archived.get(marker):
            print(f"[skip] {marker} already archived")
            continue
        if not args.confirm:
            print(f"[would archive] {marker}  {object_id}")
            continue
        post(root, token, endpoint, payload)
        archived[marker] = object_id
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        print(f"[archived] {marker}  {object_id}")

    if not args.confirm:
        print("\nDry run — nothing archived. Re-run with --confirm.")


if __name__ == "__main__":
    sys.exit(main())
