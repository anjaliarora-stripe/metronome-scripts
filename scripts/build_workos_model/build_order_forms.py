#!/usr/bin/env python3
"""Turn three signed WorkOS order forms into Metronome contracts.

Run build_workos_model.py first. This script adds the products and rates these deals
need to its "WorkOS List Price" rate card, then creates one customer and one contract
per order form:

  Businessolver, Inc.  2-year prepaid credits ($240K, then $260K) plus $60K/yr
                       Enterprise Support; an extra SSO tier at $50 above 100 connections
  Drata Inc.           Custom SSO and Directory Sync tiers, $499/mo Admin Portal
                       branding, a $30K monthly minimum subscription that funds a
                       $30K monthly usage credit
  ServiceNow, Inc.     WorkOS Enterprise at $250K/yr less 30%, with 1M MAUs and
                       50 SSO / 50 Directory Sync connections included

Contracts keep their signed dates. Usage invoices start at the Oct 1, 2026 cutover, as
if billing moved to Metronome mid-contract. Annual fees keep their real invoice dates.

    python3 scripts/build_workos_model/build_order_forms.py --dry-run
    python3 scripts/build_workos_model/build_order_forms.py

IDs are saved to the model's state.json, so archive_build.py archives these objects too.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from build_workos_model import (
    DEFAULT_STATE_FILE,
    FREE_ALLOWANCES,
    REPO_ROOT,
    Metronome,
    State,
    create_once,
    load_env,
)

CUTOVER = "2026-10-01T00:00:00Z"
RATES_START = "2026-01-01T00:00:00Z"

ORDER_FORM_PRODUCTS = {
    "workos_enterprise": {"name": "WorkOS Enterprise", "type": "SUBSCRIPTION"},
    "admin_portal_branding": {"name": "Admin Portal Custom Branding", "type": "SUBSCRIPTION"},
    "included_connections": {"name": "Included Connections", "type": "FIXED"},
    "minimum_spend": {"name": "Monthly Minimum Commitment", "type": "FIXED"},
    "minimum_fee": {"name": "Monthly Minimum", "type": "SUBSCRIPTION"},
}

# (product key, billing frequency, list price in cents)
ORDER_FORM_RATES = [
    ("workos_enterprise", "ANNUAL", 25_000_000),
    ("support_enterprise", "ANNUAL", 0),  # placeholder; each contract sets its price
    ("admin_portal_branding", "MONTHLY", 49_900),
    ("minimum_fee", "MONTHLY", 0),  # placeholder; each contract sets its price
]

BUSINESSOLVER_SSO_TIERS = [
    {"size": 15, "price": 12500},
    {"size": 15, "price": 10000},
    {"size": 20, "price": 8000},
    {"size": 50, "price": 6500},
    {"price": 5000},
]

DRATA_CONNECTION_TIERS = [
    {"size": 200, "price": 4900},
    {"size": 100, "price": 4000},
    {"size": 200, "price": 3000},
    {"size": 500, "price": 2500},
    {"size": 1000, "price": 2000},
    {"price": 1500},
]

DRATA_MINIMUM_MONTHLY_SPEND = 3_000_000  # $30,000


def tiered_price(product_id: str, tiers: list[dict], starting_at: str, ending_before: str) -> dict:
    return {
        "type": "OVERWRITE",
        "product_id": product_id,
        "starting_at": starting_at,
        "ending_before": ending_before,
        "overwrite_rate": {"rate_type": "TIERED", "tiers": tiers},
    }


def allowance(name: str, credit_product_id: str, usage_product_id: str, quantity: int,
              starting_at: str, ending_before: str) -> dict:
    # No recurrence_frequency: one credit per usage invoice (calendar month).
    # No proration: a partial first month still gets the full allowance.
    return {
        "name": name,
        "product_id": credit_product_id,
        "applicable_product_ids": [usage_product_id],
        "priority": 1,
        "access_amount": {"access_type": "QUANTITY", "unit_price": 1, "quantity": quantity},
        "commit_duration": {"value": 1, "unit": "PERIODS"},
        "starting_at": starting_at,
        "ending_before": ending_before,
        "proration": "NONE",
    }


def annual_subscription(name: str, product_id: str, anchor: str) -> dict:
    # Billed on the contract anniversary on a scheduled invoice, which it shares with
    # any commit payment invoiced the same day.
    return {
        "name": name,
        "subscription_rate": {"product_id": product_id, "billing_frequency": "ANNUAL"},
        "collection_schedule": "ADVANCE",
        "initial_quantity": 1,
        "proration": {"is_prorated": False, "invoice_behavior": "BILL_IMMEDIATELY"},
        "billing_cycle_config": {"anchor_date": anchor, "invoice_placement": "ON_SCHEDULED_INVOICE"},
    }


def businessolver(ids: dict) -> dict:
    start, year_2, end = "2026-10-07T00:00:00Z", "2027-10-07T00:00:00Z", "2028-10-07T00:00:00Z"
    return {
        "name": "Enterprise Package",
        "starting_at": start,
        "ending_before": end,
        "net_payment_terms_days": 30,
        "commits": [{
            "type": "PREPAID",
            "name": "Prepaid Credits",
            "product_id": ids["annual_commit"],
            "priority": 100,
            "rollover_fraction": 1,
            "applicable_product_ids": [
                ids[key] for key in ("sso", "dsync", "authkit_maus", "radar", "custom_domain")
            ],
            "access_schedule": {"schedule_items": [
                {"amount": 24_000_000, "starting_at": start, "ending_before": end},
                {"amount": 26_000_000, "starting_at": year_2, "ending_before": end},
            ]},
            "invoice_schedule": {"schedule_items": [
                {"amount": 24_000_000, "timestamp": start},
                {"amount": 26_000_000, "timestamp": year_2},
            ]},
        }],
        "subscriptions": [annual_subscription("Enterprise Support", ids["support_enterprise"], start)],
        "overrides": [
            tiered_price(ids["sso"], BUSINESSOLVER_SSO_TIERS, start, end),
            {
                "type": "OVERWRITE",
                "starting_at": start,
                "ending_before": end,
                "override_specifiers": [
                    {"product_id": ids["support_enterprise"], "billing_frequency": "ANNUAL"},
                ],
                "overwrite_rate": {"rate_type": "FLAT", "price": 6_000_000},
            },
        ],
        "recurring_credits": [
            allowance(name, ids[credit_product], ids[usage_product], quantity, start, end)
            for name, credit_product, usage_product, quantity in FREE_ALLOWANCES.values()
        ],
    }


def drata(ids: dict) -> dict:
    start, end = "2026-02-01T00:00:00Z", "2027-02-01T00:00:00Z"
    return {
        "name": "Enterprise Package",
        "starting_at": start,
        "ending_before": end,
        "usage_statement_schedule": {
            "frequency": "MONTHLY",
            "day": "FIRST_OF_MONTH",
            "invoice_generation_starting_at": CUTOVER,
        },
        "scheduled_charges_on_usage_invoices": "ALL",
        "overrides": [
            tiered_price(ids["sso"], DRATA_CONNECTION_TIERS, start, end),
            tiered_price(ids["dsync"], DRATA_CONNECTION_TIERS, start, end),
            {
                "type": "OVERWRITE",
                "starting_at": start,
                "ending_before": end,
                "override_specifiers": [{"product_id": ids["minimum_fee"], "billing_frequency": "MONTHLY"}],
                "overwrite_rate": {"rate_type": "FLAT", "price": DRATA_MINIMUM_MONTHLY_SPEND},
            },
        ],
        "subscriptions": [
            {
                "name": "Admin Portal Custom Branding",
                "subscription_rate": {"product_id": ids["admin_portal_branding"], "billing_frequency": "MONTHLY"},
                "collection_schedule": "ADVANCE",
                "initial_quantity": 1,
                "starting_at": CUTOVER,
                "proration": {"is_prorated": True, "invoice_behavior": "BILL_IMMEDIATELY"},
            },
            {
                "temporary_id": "monthly_minimum",
                "name": "Monthly Minimum ($30K)",
                "subscription_rate": {"product_id": ids["minimum_fee"], "billing_frequency": "MONTHLY"},
                "collection_schedule": "ARREARS",
                "initial_quantity": 1,
                "starting_at": CUTOVER,
                "proration": {"is_prorated": False, "invoice_behavior": "BILL_ON_NEXT_COLLECTION_DATE"},
            },
        ],
        # Each month's fee funds that month's usage; unused credit expires at month end.
        "recurring_credits": [{
            "name": "Monthly Minimum Credit",
            "product_id": ids["minimum_spend"],
            "applicable_product_tags": ["usage"],
            "priority": 100,
            "access_amount": {"access_type": "SPEND", "unit_price": DRATA_MINIMUM_MONTHLY_SPEND},
            "commit_duration": {"value": 1, "unit": "PERIODS"},
            "starting_at": CUTOVER,
            "ending_before": end,
            "proration": "NONE",
            "subscription_config": {
                "subscription_id": "monthly_minimum",
                "allocation": "POOLED",
                "apply_seat_increase_config": {"is_prorated": False},
            },
        }],
    }


def servicenow(ids: dict) -> dict:
    start, end = "2026-09-21T00:00:00Z", "2028-09-21T00:00:00Z"
    return {
        "name": "WorkOS Enterprise",
        "starting_at": start,
        "ending_before": end,
        "net_payment_terms_days": 30,
        "usage_statement_schedule": {
            "frequency": "MONTHLY",
            "day": "FIRST_OF_MONTH",
            "invoice_generation_starting_at": CUTOVER,
        },
        "subscriptions": [annual_subscription("WorkOS Enterprise", ids["workos_enterprise"], start)],
        "overrides": [{
            "type": "MULTIPLIER",
            "multiplier": 0.7,
            "starting_at": start,
            "ending_before": end,
            "override_specifiers": [{"product_id": ids["workos_enterprise"], "billing_frequency": "ANNUAL"}],
        }],
        "recurring_credits": [
            allowance("AuthKit MAUs", ids["authkit_free_maus"], ids["authkit_maus"], 1_000_000, CUTOVER, end),
            allowance("SSO Connections", ids["included_connections"], ids["sso"], 50, CUTOVER, end),
            allowance("Directory Sync Connections", ids["included_connections"], ids["dsync"], 50, CUTOVER, end),
        ],
    }


# state key -> (customer name, ingest alias, contract terms)
ORDER_FORMS = {
    "businessolver": ("Businessolver, Inc.", "team_businessolver", businessolver),
    "drata": ("Drata Inc.", "team_drata", drata),
    "servicenow": ("ServiceNow, Inc.", "team_servicenow", servicenow),
}


def build(api: Metronome, state: State) -> None:
    rate_card_id = state.get("rate_card", "id")
    if not rate_card_id:
        raise SystemExit(f"No rate card in {state.path}. Run build_workos_model.py first.")

    print("\n== 1. Order form products ==")
    for key, payload in ORDER_FORM_PRODUCTS.items():
        create_once(
            api, state, "products", key, payload["name"],
            "/v1/contract-pricing/products/create", payload,
        )
    ids = dict(state.data["products"])

    print("\n== 2. Rates on WorkOS List Price ==")
    missing = [rate for rate in ORDER_FORM_RATES if not state.get("rate_card", f"rate_added.{rate[0]}")]
    if not missing:
        print("[skip] Order form rates already added")
    else:
        rates = [
            {
                "product_id": ids[key],
                "starting_at": RATES_START,
                "entitled": True,
                "rate_type": "FLAT",
                "price": price,
                "billing_frequency": frequency,
            }
            for key, frequency, price in missing
        ]
        print(f"[create] {len(rates)} order form rates")
        api.post(
            "/v1/contract-pricing/rate-cards/addRates",
            {"rate_card_id": rate_card_id, "rates": rates},
        )
        for key, _, _ in missing:
            state.set("rate_card", f"rate_added.{key}", "true")

    print("\n== 3. Customers and contracts ==")
    for key, (name, alias, contract_terms) in ORDER_FORMS.items():
        customer_id = create_once(
            api, state, "customers", key, name,
            "/v1/customers", {"name": name, "ingest_aliases": [alias]},
        )
        create_once(
            api, state, "contracts", key, f"{name} contract",
            "/v1/contracts/create",
            {"customer_id": customer_id, "rate_card_id": rate_card_id, **contract_terms(ids)},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Metronome contracts from three WorkOS order forms.")
    parser.add_argument("--dry-run", action="store_true", help="Print every payload without sending it")
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help="State file written by build_workos_model.py (default: state.json)",
    )
    args = parser.parse_args()

    load_env(REPO_ROOT / ".env")
    token = os.environ.get("METRONOME_API_TOKEN", "").strip()
    base_url = os.environ.get("METRONOME_BASE_URL", "https://api.metronome.com/v1").strip()
    if not args.dry_run and not token:
        raise SystemExit("METRONOME_API_TOKEN is missing. Add it to .env.")

    api = Metronome(base_url, token, args.dry_run)
    state = State(Path(args.state_file), args.dry_run)
    if args.dry_run and state.path.exists():
        state.data = json.loads(state.path.read_text())

    print("Building WorkOS order form contracts" + (" (dry run)" if args.dry_run else ""))
    build(api, state)

    if args.dry_run:
        print("\nDry run — nothing sent.")
    else:
        print(f"\nDone. IDs saved to {state.path}")


if __name__ == "__main__":
    main()
