#!/usr/bin/env python3
"""Build the WorkOS pricing model in Metronome.

Creates, in order:
  1. Billable metrics (6 streaming: 5 SUM of quantity, 1 MAX), all grouped by a required org_id
  2. Products (6 usage tagged "usage" with one invoice line per org_id, 3 subscription,
     3 fixed)
  3. Rate card "WorkOS List Price" and its rates
  4. Packages: Pay as You Go, Annual Credits (prepaid commit, no scope), both with
     monthly quantity credits for 1M free AuthKit MAUs and 1,000 free Radar checks
  5. Customers: Acme Labs, Globex Corp
  6. Contracts from each package
  7. Contract edits: Custom Domain on Acme Labs, 20% SSO/Directory Sync discount on Globex Corp

Run from the repo root:

    python3 scripts/build_workos_model/build_workos_model.py --dry-run
    python3 scripts/build_workos_model/build_workos_model.py

Every created ID is saved to state.json next to this script (state-<suffix>.json
with --suffix). Re-running skips steps already in that file, so a failed run can
be resumed. Use --suffix to build a second, independent copy (names, aliases and
the state file get the suffix).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAX_RETRIES = 5
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_STATE_FILE = SCRIPT_DIR / "state.json"

PRODUCTION = {"name": "environment", "in_values": ["production"]}

CONNECTION_TIERS = [
    {"size": 15, "price": 12500},
    {"size": 15, "price": 10000},
    {"size": 20, "price": 8000},
    {"price": 6500},
]


ORG_ID = ["org_id"]
# Every usage product shows one invoice line per org. Tiers restart and per-1M rounding
# applies for each org, so SSO and Directory Sync tiers count each org's connections.
BY_ORG = {"presentation_group_key": ORG_ID}


def sum_metric(name: str, event_type: str, id_property: str) -> dict:
    # Sums each event's quantity (usually 1; a batch can carry more). Distinct counts rely
    # on WorkOS sending one event per ID per month, enforced by a transaction_id that
    # includes the ID and the month (Metronome drops repeats).
    # Group keys must be required properties, so events without an org_id don't count.
    return {
        "name": name,
        "aggregation_type": "SUM",
        "aggregation_key": "quantity",
        "event_type_filter": {"in_values": [event_type]},
        "property_filters": [
            {"name": "quantity", "exists": True},
            {"name": id_property, "exists": True},
            {"name": "org_id", "exists": True},
            PRODUCTION,
        ],
        "group_keys": [ORG_ID],
    }


BILLABLE_METRICS = {
    "authkit_maus": sum_metric("AuthKit MAUs", "user_activity", "user_id"),
    "sso_connections": sum_metric("SSO connections", "sso_connection", "connection_id"),
    "dsync_connections": sum_metric("Directory Sync connections", "dsync_connection", "connection_id"),
    "audit_log_streams": sum_metric("Audit log streams", "audit_log_stream", "stream_id"),
    "radar_checks": sum_metric("Radar checks", "radar_check", "check_id"),
    "audit_log_events_stored": {
        "name": "Audit log events stored",
        "aggregation_type": "MAX",
        "aggregation_key": "events_stored",
        "event_type_filter": {"in_values": ["audit_log_storage_snapshot"]},
        # One snapshot per org; the product sums each org's monthly peak.
        "property_filters": [
            {"name": "events_stored", "exists": True},
            {"name": "org_id", "exists": True},
            PRODUCTION,
        ],
        "group_keys": [ORG_ID],
    },
}

ROUND_UP = {"rounding_method": "ROUND_UP", "decimal_places": 0}
# Every usage product gets this tag so credits and commits can scope to all usage.
USAGE_TAGS = ["usage"]

# key -> (product payload, billable metric key or None)
PRODUCTS = {
    "authkit_maus": ({"name": "AuthKit MAUs", "type": "USAGE", **BY_ORG}, "authkit_maus"),
    "sso": ({"name": "Single Sign-On", "type": "USAGE", **BY_ORG}, "sso_connections"),
    "dsync": ({"name": "Directory Sync", "type": "USAGE", **BY_ORG}, "dsync_connections"),
    "audit_log_streaming": (
        {"name": "Audit Log Streaming", "type": "USAGE", **BY_ORG},
        "audit_log_streams",
    ),
    "radar": ({"name": "Radar", "type": "USAGE", **BY_ORG}, "radar_checks"),
    "audit_log_retention": (
        {
            "name": "Audit Log Retention",
            "type": "USAGE",
            "quantity_conversion": {
                "conversion_factor": 1_000_000,
                "operation": "DIVIDE",
                "name": "1M events stored",
            },
            "quantity_rounding": ROUND_UP,
            **BY_ORG,
        },
        "audit_log_events_stored",
    ),
    "custom_domain": ({"name": "Custom Domain", "type": "SUBSCRIPTION"}, None),
    "support_scale": ({"name": "Support: Scale", "type": "SUBSCRIPTION"}, None),
    "support_enterprise": (
        {"name": "Support: Enterprise", "type": "SUBSCRIPTION"},
        None,
    ),
    "annual_commit": ({"name": "Annual Prepaid Commit", "type": "FIXED"}, None),
    "authkit_free_maus": ({"name": "AuthKit Free MAUs", "type": "FIXED"}, None),
    "radar_free_checks": ({"name": "Radar Free Checks", "type": "FIXED"}, None),
}

# key -> rate fields (prices in cents)
RATES = {
    "authkit_maus": {"rate_type": "FLAT", "price": 0.25},  # $2,500 per 1M MAUs
    "sso": {"rate_type": "TIERED", "tiers": CONNECTION_TIERS},
    "dsync": {"rate_type": "TIERED", "tiers": CONNECTION_TIERS},
    "audit_log_streaming": {"rate_type": "FLAT", "price": 12500},
    "radar": {"rate_type": "FLAT", "price": 0.2},
    "audit_log_retention": {"rate_type": "FLAT", "price": 9900},
    "custom_domain": {"rate_type": "FLAT", "price": 9900, "billing_frequency": "MONTHLY"},
    "support_scale": {"rate_type": "FLAT", "price": 100000, "billing_frequency": "MONTHLY"},
    "support_enterprise": {"rate_type": "FLAT", "price": 0, "billing_frequency": "MONTHLY"},
}

# Monthly free allowances, granted as quantity credits on every package.
# key -> (credit name, fixed product key, usage product key, quantity in billed units)
FREE_ALLOWANCES = {
    "authkit": ("AuthKit MAUs", "authkit_free_maus", "authkit_maus", 1_000_000),
    "radar": ("Radar Checks", "radar_free_checks", "radar", 1000),
}

ANNUAL_COMMIT_ACCESS = 10_000_000  # $100,000 of balance
ANNUAL_COMMIT_INVOICE = 9_000_000  # $90,000 invoiced at start
NEGOTIATED_MULTIPLIER = 0.8  # 20% off SSO and Directory Sync


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class Metronome:
    def __init__(self, base_url: str, token: str, dry_run: bool):
        self.root = base_url.rstrip("/").removesuffix("/v1")
        self.token = token
        self.dry_run = dry_run
        self._fake_ids = 0

    def post(self, path: str, payload: dict) -> dict:
        url = f"{self.root}{path}"
        if self.dry_run:
            self._fake_ids += 1
            print(f"  POST {path}\n{json.dumps(payload, indent=2)}")
            return {"data": {"id": f"dry-run-id-{self._fake_ids}"}}

        body = json.dumps(payload).encode("utf-8")
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return json.loads(response.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 or 500 <= exc.code <= 599:
                    last_error = f"HTTP {exc.code}: {detail}"
                    sleep_s = min(2 ** (attempt - 1), 16)
                    print(f"Retryable error ({last_error}). Retrying in {sleep_s}s...")
                    time.sleep(sleep_s)
                    continue
                raise SystemExit(
                    f"POST {path} failed with HTTP {exc.code}: {detail}\n"
                    f"Payload: {json.dumps(payload)}"
                ) from exc
            except urllib.error.URLError as exc:
                last_error = str(exc.reason)
                sleep_s = min(2 ** (attempt - 1), 16)
                print(f"Network error ({last_error}). Retrying in {sleep_s}s...")
                time.sleep(sleep_s)
        raise SystemExit(f"POST {path} failed after {MAX_RETRIES} retries: {last_error}")


class State:
    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self.data: dict = {}
        if path.exists() and not dry_run:
            self.data = json.loads(path.read_text())

    def get(self, section: str, key: str) -> str | None:
        return self.data.get(section, {}).get(key)

    def set(self, section: str, key: str, value: str) -> None:
        self.data.setdefault(section, {})[key] = value
        if not self.dry_run:
            self.path.write_text(json.dumps(self.data, indent=2) + "\n")


def create_once(api: Metronome, state: State, section: str, key: str, label: str,
                path: str, payload: dict) -> str:
    existing = state.get(section, key)
    if existing:
        print(f"[skip] {label} already created: {existing}")
        return existing
    print(f"[create] {label}")
    response = api.post(path, payload)
    new_id = response.get("data", {}).get("id")
    if not new_id:
        raise SystemExit(f"No id returned for {label}: {json.dumps(response)}")
    state.set(section, key, new_id)
    print(f"         id={new_id}")
    return new_id


def first_of_month_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01T00:00:00Z")


def build(api: Metronome, state: State, start: str, suffix: str) -> None:
    tag = f" {suffix}" if suffix else ""
    alias_tag = f"-{suffix}" if suffix else ""

    print("\n== 1. Billable metrics ==")
    metric_ids = {}
    for key, payload in BILLABLE_METRICS.items():
        body = {**payload, "name": payload["name"] + tag}
        metric_ids[key] = create_once(
            api, state, "billable_metrics", key, body["name"],
            "/v1/billable-metrics/create", body,
        )

    print("\n== 2. Products ==")
    product_ids = {}
    for key, (payload, metric_key) in PRODUCTS.items():
        body = {**payload, "name": payload["name"] + tag}
        if metric_key:
            body["billable_metric_id"] = metric_ids[metric_key]
        if body["type"] == "USAGE":
            body["tags"] = USAGE_TAGS
        product_ids[key] = create_once(
            api, state, "products", key, body["name"],
            "/v1/contract-pricing/products/create", body,
        )

    print("\n== 3. Rate card ==")
    rate_card_name = "WorkOS List Price" + tag
    rate_card_id = create_once(
        api, state, "rate_card", "id", rate_card_name,
        "/v1/contract-pricing/rate-cards/create", {"name": rate_card_name},
    )
    if state.get("rate_card", "rates_added"):
        print("[skip] Rates already added")
    else:
        rates = [
            {"product_id": product_ids[key], "starting_at": start, "entitled": True, **fields}
            for key, fields in RATES.items()
        ]
        print(f"[create] {len(rates)} rates on {rate_card_name}")
        api.post(
            "/v1/contract-pricing/rate-cards/addRates",
            {"rate_card_id": rate_card_id, "rates": rates},
        )
        state.set("rate_card", "rates_added", "true")

    print("\n== 4. Packages ==")
    payg_alias = "pay-as-you-go" + alias_tag
    annual_alias = "annual-credits" + alias_tag
    free_allowance_credits = [
        {
            "name": name,
            "product_id": product_ids[credit_product],
            "applicable_product_ids": [product_ids[usage_product]],
            "priority": 1,
            "access_amount": {"access_type": "QUANTITY", "unit_price": 1, "quantity": quantity},
            "commit_duration": {"value": 1, "unit": "PERIODS"},
            "recurrence_frequency": "MONTHLY",
            "starting_at_offset": {"value": 0, "unit": "MONTHS"},
        }
        for name, credit_product, usage_product, quantity in FREE_ALLOWANCES.values()
    ]
    create_once(
        api, state, "packages", "pay_as_you_go", "Pay as You Go package",
        "/v1/packages/create",
        {
            "name": "Pay as You Go" + tag,
            "rate_card_id": rate_card_id,
            "aliases": [{"name": payg_alias}],
            "usage_statement_schedule": {"frequency": "MONTHLY", "day": "FIRST_OF_MONTH"},
            "recurring_credits": free_allowance_credits,
        },
    )
    create_once(
        api, state, "packages", "annual_credits", "Annual Credits package",
        "/v1/packages/create",
        {
            "name": "Annual Credits" + tag,
            "rate_card_id": rate_card_id,
            "aliases": [{"name": annual_alias}],
            "duration": {"value": 12, "unit": "MONTHS"},
            "usage_statement_schedule": {"frequency": "MONTHLY", "day": "FIRST_OF_MONTH"},
            "recurring_credits": free_allowance_credits,
            "commits": [
                {
                    "type": "PREPAID",
                    "name": "Annual Credits",
                    "product_id": product_ids["annual_commit"],
                    "priority": 100,
                    "access_schedule": {
                        "schedule_items": [
                            {
                                "amount": ANNUAL_COMMIT_ACCESS,
                                "starting_at_offset": {"value": 0, "unit": "MONTHS"},
                                "duration": {"value": 12, "unit": "MONTHS"},
                            }
                        ]
                    },
                    "invoice_schedule": {
                        "schedule_items": [
                            {
                                "unit_price": ANNUAL_COMMIT_INVOICE,
                                "quantity": 1,
                                "date_offset": {"value": 0, "unit": "DAYS"},
                            }
                        ]
                    },
                }
            ],
        },
    )

    print("\n== 5. Customers ==")
    acme_id = create_once(
        api, state, "customers", "acme_labs", "Acme Labs",
        "/v1/customers",
        {"name": "Acme Labs" + tag, "ingest_aliases": ["team_acme_labs" + alias_tag]},
    )
    globex_id = create_once(
        api, state, "customers", "globex_corp", "Globex Corp",
        "/v1/customers",
        {"name": "Globex Corp" + tag, "ingest_aliases": ["team_globex_corp" + alias_tag]},
    )

    print("\n== 6. Contracts from packages ==")
    acme_contract = create_once(
        api, state, "contracts", "acme_labs", "Acme Labs contract (Pay as You Go)",
        "/v1/contracts/create",
        {"customer_id": acme_id, "starting_at": start, "package_alias": payg_alias},
    )
    globex_contract = create_once(
        api, state, "contracts", "globex_corp", "Globex Corp contract (Annual Credits)",
        "/v1/contracts/create",
        {"customer_id": globex_id, "starting_at": start, "package_alias": annual_alias},
    )

    print("\n== 7. Contract edits ==")
    create_once(
        api, state, "edits", "acme_custom_domain", "Add Custom Domain to Acme Labs",
        "/v2/contracts/edit",
        {
            "customer_id": acme_id,
            "contract_id": acme_contract,
            "add_subscriptions": [
                {
                    "name": "Custom Domain",
                    "subscription_rate": {
                        "product_id": product_ids["custom_domain"],
                        "billing_frequency": "MONTHLY",
                    },
                    "collection_schedule": "ADVANCE",
                    "initial_quantity": 1,
                    "starting_at": start,
                    "proration": {"is_prorated": True, "invoice_behavior": "BILL_IMMEDIATELY"},
                }
            ],
        },
    )
    create_once(
        api, state, "edits", "globex_discount", "Add 20% SSO / Directory Sync discount to Globex Corp",
        "/v2/contracts/edit",
        {
            "customer_id": globex_id,
            "contract_id": globex_contract,
            "add_overrides": [
                {
                    "type": "MULTIPLIER",
                    "multiplier": NEGOTIATED_MULTIPLIER,
                    "starting_at": start,
                    "override_specifiers": [
                        {"product_id": product_ids["sso"]},
                        {"product_id": product_ids["dsync"]},
                    ],
                }
            ],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the WorkOS pricing model in Metronome.")
    parser.add_argument("--dry-run", action="store_true", help="Print every payload without sending it")
    parser.add_argument(
        "--start",
        default=first_of_month_utc(),
        help="Rate and contract start, RFC 3339 at midnight UTC (default: first of this month)",
    )
    parser.add_argument("--suffix", default="", help="Appended to names and aliases to build a separate copy")
    parser.add_argument(
        "--state-file",
        help="Where created IDs are saved (default: state.json, or state-<suffix>.json with --suffix)",
    )
    args = parser.parse_args()
    if not args.state_file:
        name = f"state-{args.suffix}.json" if args.suffix else DEFAULT_STATE_FILE.name
        args.state_file = str(SCRIPT_DIR / name)

    load_env(REPO_ROOT / ".env")
    token = os.environ.get("METRONOME_API_TOKEN", "").strip()
    base_url = os.environ.get("METRONOME_BASE_URL", "https://api.metronome.com/v1").strip()
    if not args.dry_run and not token:
        raise SystemExit("METRONOME_API_TOKEN is missing. Add it to .env.")

    api = Metronome(base_url, token, args.dry_run)
    state = State(Path(args.state_file), args.dry_run)

    print(f"Building WorkOS model starting {args.start}" + (" (dry run)" if args.dry_run else ""))
    build(api, state, args.start, args.suffix)

    if args.dry_run:
        print("\nDry run — nothing sent.")
    else:
        print(f"\nDone. IDs saved to {state.path}")


if __name__ == "__main__":
    sys.exit(main())
