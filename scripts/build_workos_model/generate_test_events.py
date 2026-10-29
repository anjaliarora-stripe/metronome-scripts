#!/usr/bin/env python3
"""Generate test usage events for the WorkOS model customers.

Writes one events file per customer, in the format send_usage.py reads:

    python3 scripts/build_workos_model/generate_test_events.py
    python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_acme_labs.json
    python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_globex_corp.json

The order form customers from build_order_forms.py (events_team_businessolver.json,
events_team_drata.json, events_team_servicenow.json) are written too, unless --suffix is set.

transaction_ids follow the guidance for WorkOS, {customer}_{event_type}_{unique key}:
one ID per user / connection / stream per calendar month (UTC), so Metronome keeps the
first event and drops every repeat. Counted events carry quantity 1, which the SUM
metrics add up. Every user and connection is sent twice to prove the repeats are
dropped. Re-sending a file is also deduplicated.

Each customer has two orgs of its own, assigned round-robin through org_id, and every
production event carries one (storage snapshots are sent per org). Every usage product
shows one invoice line per org, and connection tiers restart for each org.

Expected charges (AuthKit at $0.0025 per MAU stays inside the free 1M MAUs credit):
  Acme Labs    SSO 10 + 10 = $2,500, Directory Sync 3 + 2 = $625,
               Radar 26,000 - 1,000 free = 25,000 x $0.002 = $50, Custom Domain $99  -> $3,274
  Globex Corp  SSO 30 + 30 = $6,750 x 0.8 = $5,400, Directory Sync 10 + 10 = $2,500 x 0.8 = $2,000,
               Audit log streams 1 + 1 = $250, peak stored 2.0M and 1.5M = 2 + 2 blocks = $396,
               Radar 500 (all free) -> $8,046 drawn from the $100,000 commit
  Businessolver  SSO 60 + 60 on its tiers = $11,250, Directory Sync 20 + 20 = $4,750,
                 Radar 21,000 - 1,000 free = $40 -> $16,040 drawn from Prepaid Credits
  Drata          SSO 150 + 150 = $14,700, Directory Sync 75 + 75 = $7,350 on its tiers,
                 covered by the $30,000 minimum credit -> $30,000 subscription + $499 branding
  ServiceNow     SSO 30 + 30 = $6,750 - 50 included ($5,750) = $1,000; Directory Sync
                 23 + 22 and 500 MAUs are inside the included amounts -> $1,000
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


class Customer:
    def __init__(self, alias: str, when: datetime, orgs: tuple[str, ...]):
        self.alias = alias
        self.when = when
        self.month = when.strftime("%Y-%m")
        self.orgs = orgs

    def ts(self, minutes: int = 0) -> str:
        return (self.when + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def org(self, n: int) -> str:
        return self.orgs[(n - 1) % len(self.orgs)]

    def scoped_id(self, prefix: str, n: int, width: int) -> str:
        # IDs carry the org, so they read like WorkOS IDs and stay unique per customer.
        return f"{prefix}_{self.org(n).removeprefix('org_')}_{n:0{width}d}"

    def event(self, event_type: str, unique_key: str, ts: str, environment: str = "production",
              **properties) -> dict:
        # transaction_id = {customer}_{event_type}_{unique key}; staging gets its own IDs.
        suffix = "" if environment == "production" else "_staging"
        return {
            "transaction_id": f"{self.alias}_{event_type}_{unique_key}{suffix}",
            "event_type": event_type,
            "timestamp": ts,
            "properties": {"environment": environment, **properties},
        }

    def users(self, count: int, environment: str = "production") -> list[dict]:
        # Sent twice (sign in, then profile update): same monthly transaction_id, counted once.
        return [
            self.event(
                "user_activity", f"{user_id}_{self.month}", self.ts(minutes), environment,
                user_id=user_id, org_id=self.org(i), action=action, quantity=1,
            )
            for i in range(1, count + 1)
            for user_id in [self.scoped_id("usr", i, 4)]
            for minutes, action in ((0, "sign_in"), (30, "profile_update"))
        ]

    def _connections(self, kind: str, count: int, environment: str) -> list[dict]:
        # Daily pings collapse into one event per connection per month.
        return [
            self.event(
                f"{kind}_connection", f"{connection_id}_{self.month}", self.ts(minutes), environment,
                connection_id=connection_id, org_id=self.org(i), quantity=1,
            )
            for i in range(1, count + 1)
            for connection_id in [self.scoped_id(f"conn_{kind}", i, 3)]
            for minutes in (0, 60)
        ]

    def sso_connections(self, count: int, environment: str = "production") -> list[dict]:
        return self._connections("sso", count, environment)

    def dsync_connections(self, count: int, environment: str = "production") -> list[dict]:
        return self._connections("dsync", count, environment)

    def streams(self, count: int) -> list[dict]:
        return [
            self.event(
                "audit_log_stream", f"{stream_id}_{self.month}", self.ts(),
                stream_id=stream_id, org_id=self.org(i), quantity=1,
            )
            for i in range(1, count + 1)
            for stream_id in [self.scoped_id("stream", i, 1)]
        ]

    def radar(self, count: int) -> list[dict]:
        return [
            self.event("radar_check", check_id, self.ts(), check_id=check_id, org_id=self.org(i), quantity=1)
            for i in range(1, count + 1)
            for check_id in [self.scoped_id("chk", i, 5)]
        ]

    def storage(self, days_ago: int, *events_stored_per_org: int) -> list[dict]:
        # One running total per org, in the order of self.orgs.
        day = self.when - timedelta(days=days_ago)
        return [
            self.event(
                "audit_log_storage_snapshot", f"{org_id}_{day.strftime('%Y-%m-%d')}",
                day.strftime("%Y-%m-%dT%H:%M:%SZ"), events_stored=events_stored, org_id=org_id,
            )
            for org_id, events_stored in zip(self.orgs, events_stored_per_org)
        ]

    def staging_noise(self) -> list[dict]:
        # Staging is free; these must not change any invoice line.
        return self.users(1, "staging") + self.sso_connections(1, "staging")


def acme_labs(c: Customer) -> list[dict]:
    return (
        c.users(50)
        + c.sso_connections(20)
        + c.dsync_connections(5)
        + c.radar(26_000)
        + c.staging_noise()
    )


def globex_corp(c: Customer) -> list[dict]:
    return (
        c.users(100)
        + c.sso_connections(60)
        + c.dsync_connections(20)
        + c.streams(2)
        + c.storage(1, 1_200_000, 800_000)
        + c.storage(0, 2_000_000, 1_500_000)
        + c.radar(500)
        + c.staging_noise()
    )


def businessolver(c: Customer) -> list[dict]:
    return (
        c.users(300)
        + c.sso_connections(120)
        + c.dsync_connections(40)
        + c.radar(21_000)
        + c.staging_noise()
    )


def drata(c: Customer) -> list[dict]:
    return c.sso_connections(300) + c.dsync_connections(150) + c.staging_noise()


def servicenow(c: Customer) -> list[dict]:
    return (
        c.users(500)
        + c.sso_connections(60)
        + c.dsync_connections(45)
        + c.staging_noise()
    )


# (ingest alias, the customer's own orgs in WorkOS, events)
PACKAGE_CUSTOMERS = (
    ("team_acme_labs", ("org_hooli", "org_initech"), acme_labs),
    ("team_globex_corp", ("org_soylent", "org_vandelay"), globex_corp),
)
ORDER_FORM_CUSTOMERS = (
    ("team_businessolver", ("org_wonka", "org_stark"), businessolver),
    ("team_drata", ("org_pied_piper", "org_aviato"), drata),
    ("team_servicenow", ("org_cyberdyne", "org_tyrell"), servicenow),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate WorkOS test usage events.")
    default_when = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    parser.add_argument(
        "--timestamp",
        default=default_when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        help="Base event timestamp, RFC 3339 (default: two hours ago, on the hour, UTC)",
    )
    parser.add_argument("--suffix", default="", help="Customer alias suffix used by build_workos_model.py")
    args = parser.parse_args()
    when = datetime.strptime(args.timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    alias_tag = f"-{args.suffix}" if args.suffix else ""

    customers = [(base + alias_tag, orgs, build_events) for base, orgs, build_events in PACKAGE_CUSTOMERS]
    if not args.suffix:
        customers += ORDER_FORM_CUSTOMERS

    for alias, orgs, build_events in customers:
        events = build_events(Customer(alias, when, orgs))
        path = SCRIPT_DIR / f"events_{alias}.json"
        path.write_text(json.dumps({"customer_id": alias, "events": events}, indent=2) + "\n")
        unique = len({e["transaction_id"] for e in events})
        print(f"Wrote {len(events)} events ({unique} unique transaction_ids) to {path}")


if __name__ == "__main__":
    main()
