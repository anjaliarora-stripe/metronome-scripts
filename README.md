# Metronome SA helper scripts

A collection of scripts for Solution Architects working with [Metronome](https://docs.metronome.com/). Shared auth lives in `.env`.

## Layout

```
.env                         # API token and shared defaults (gitignored)
scripts/send_usage/          # Send usage events
  send_usage.py
  events.json
scripts/build_workos_model/  # Build the WorkOS pricing model end to end
  build_workos_model.py
  build_order_forms.py
  generate_test_events.py
  archive_build.py
.cursor/skills/              # Metronome Cursor skills
```

Add new scripts under `scripts/<name>/` and list them in [Scripts](#scripts).

## Prerequisites

- Python 3 (stdlib only unless a script says otherwise)
- A Metronome API token from **Developer → API tokens** in the dashboard

The API base URL is always `https://api.metronome.com/v1`. Sandbox vs production is selected by the token, not the host.

## Setup

Create `.env` in the repo root (already gitignored):

```
METRONOME_API_TOKEN=your_token_here
METRONOME_BASE_URL=https://api.metronome.com/v1
METRONOME_CUSTOMER_ID=
```

`METRONOME_CUSTOMER_ID` is an optional default customer UUID or ingest alias. Individual scripts may override it.

## Scripts


| Script                                | What it does                              | Input         |
| ------------------------------------- | ----------------------------------------- | ------------- |
| [`scripts/send_usage`](#send-usage-events) | Ingest usage events via `POST /v1/ingest` | `scripts/send_usage/events.json` |
| [`scripts/build_workos_model`](#build-the-workos-pricing-model) | Create WorkOS metrics, products, rate card, packages, customers, and contracts; [encode signed order forms as contracts](#build-the-workos-order-form-contracts); generate test events; archive a build | None (config in script) |

Run any script from the repo root:

```bash
python3 scripts/<name>/<script>.py
```

---



### Send usage events

Sends one or more usage events from `scripts/send_usage/events.json`. Edit that file between runs, then execute the script again.

**Also requires**

- A Metronome customer ID or ingest alias
- A billable metric whose `event_type` matches the events you send (events with no matching metric are accepted, then silently ignored)

**Run**

```bash
python3 scripts/send_usage/send_usage.py --dry-run
python3 scripts/send_usage/send_usage.py
python3 scripts/send_usage/send_usage.py --file path/to/other-events.json
```

Each request can include up to 100 events. Larger files are split automatically.

`scripts/send_usage/events.json`

Top-level `customer_id` applies to every event unless an event sets its own.

```json
{
  "customer_id": "sa-onboarding-demo",
  "events": [
    {
      "event_type": "api_call",
      "properties": {
        "model": "gpt-4",
        "region": "us-east-1",
        "tokens": "1500"
      }
    },
    {
      "event_type": "api_call",
      "customer_id": "another-customer-or-alias",
      "properties": {
        "model": "gpt-4o",
        "region": "us-west-2",
        "tokens": "800"
      }
    }
  ]
}
```


| Field            | Required                     | Notes                                                                |
| ---------------- | ---------------------------- | -------------------------------------------------------------------- |
| `event_type`     | Yes                          | Must match a billable metric                                         |
| `customer_id`    | Yes (file, event, or `.env`) | Metronome customer ID or ingest alias                                |
| `properties`     | No                           | Metric filters and grouping dimensions. Numbers are sent as strings. |
| `timestamp`      | No                           | RFC 3339, e.g. `2026-09-09T15:20:23Z`. Defaults to now (UTC).        |
| `transaction_id` | No                           | Idempotency key (max 128 chars). Generated per run if omitted.       |


You can also use a bare JSON array of events instead of the `{ "customer_id", "events" }` wrapper.

**Idempotency**

Metronome deduplicates on `transaction_id` for 34 days. Sending the same ID again does **not** update usage. By default this script generates a new ID each run, so quantity edits ingest as new usage. Set `transaction_id` yourself only when you want a safe retry of the same event.

**Verify**

1. Open **Events** in the Metronome dashboard.
2. Confirm each event matched a customer and a billable metric.
3. If the event ingested but did not affect an invoice, check that the customer has a contract, the metric is on a product/rate card, and property values (for example `model`) match the rate card group keys.

Ingest aliases can be used before the customer exists. Events sit unmatched until a customer is created with that alias.

Docs: [Send usage events](https://docs.metronome.com/guides/events/send-usage-events) · [Ingest API](https://docs.metronome.com/api-reference/usage/ingest-events) · [Design usage events](https://docs.metronome.com/guides/events/design-usage-events)

---

### Build the WorkOS pricing model

Builds the [WorkOS pricing](https://workos.com/pricing) model end to end:

1. 6 streaming billable metrics: SUM of `quantity` for MAUs, SSO connections, Directory Sync connections, audit log streams, and Radar checks; MAX for audit log events stored. All six are grouped by `org_id`. SSO and Directory Sync have separate event types, so neither metric filters on a connection type.
2. 12 products: 6 usage, 3 subscription, 3 fixed (annual commit, AuthKit Free MAUs, Radar Free Checks). Every usage product is tagged `usage`, so commits and credits can later be scoped with `applicable_product_tags: ["usage"]`.
3. Rate card `WorkOS List Price` with all list prices (AuthKit $0.0025 per MAU, which is $2,500 per 1M; Radar $0.002 per check)
4. Packages `pay-as-you-go` and `annual-credits` (a $100K prepaid commit invoiced at $90K, with no product scope). Both include monthly quantity credits for the free tiers: 1M AuthKit MAUs and 1,000 Radar checks, named `AuthKit MAUs` and `Radar Checks`. Credit names leave out the amount, since a contract can change it.
5. Customers `Acme Labs` and `Globex Corp` (ingest aliases `team_acme_labs` and `team_globex_corp`)
6. One contract from each package
7. Contract edits: Custom Domain added to Acme Labs, 20% off SSO and Directory Sync for Globex Corp

**Also requires**

- Contract editing enabled on the Metronome account

**Invoice lines by org**

Every usage product uses `org_id` as a presentation group key, so the invoice shows one line per org. Metronome prices each org's line on its own:

- SSO and Directory Sync tiers restart for each org. Two orgs with 10 connections each pay 20 x $125, not 15 x $125 + 5 x $100.
- Audit Log Retention rounds each org up to its own 1M block, and bills each org's peak storage. A grouped MAX metric on an unsplit product would bill only the single largest org's snapshot.

Metronome requires every group key to be a required property, so `org_id` must be on every event, including storage snapshots (one per org). Events without it aren't billed. For users who don't belong to an org, WorkOS should send a placeholder value.

**Transaction IDs do the distinct counting**

Metronome keeps the first event per `transaction_id` and drops repeats for 34 days. WorkOS should build each ID as `{customer}_{event_type}_{unique key}`, where the key includes the billing month (UTC) for anything billed per month. Every counted event carries `quantity: 1`, so a SUM metric equals a distinct count. A batch, such as 500 Radar checks in one event, can carry a larger quantity.

| Event | `transaction_id` |
| ----- | ---------------- |
| `user_activity` | `{customer}_user_activity_{user_id}_{YYYY-MM}` |
| `sso_connection` | `{customer}_sso_connection_{connection_id}_{YYYY-MM}` |
| `dsync_connection` | `{customer}_dsync_connection_{connection_id}_{YYYY-MM}` |
| `audit_log_stream` | `{customer}_audit_log_stream_{stream_id}_{YYYY-MM}` |
| `radar_check` | `{customer}_radar_check_{check_id}` |
| `audit_log_storage_snapshot` | `{customer}_audit_log_storage_snapshot_{org_id}_{YYYY-MM-DD}` |

Create a streaming metric before sending the events it should count. Events that arrive while a metric is being created aren't added to it, and resending them under the same `transaction_id` is dropped as a repeat.

**Run**

```bash
python3 scripts/build_workos_model/build_workos_model.py --dry-run
python3 scripts/build_workos_model/build_workos_model.py
python3 scripts/build_workos_model/build_workos_model.py --start 2026-11-01T00:00:00Z --suffix test2
```

`--start` sets when rates and contracts begin (default: first of the current month, UTC). `--suffix` appends to every name and alias so you can build a separate copy.

Created IDs are saved to `scripts/build_workos_model/state-{suffix}.json` (or `state.json` with no suffix; gitignored). Re-running skips anything already in that file, so a failed run resumes where it stopped.

**Test usage**

```bash
python3 scripts/build_workos_model/generate_test_events.py
python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_acme_labs.json
python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_globex_corp.json
```

Every user and connection is sent twice to prove that repeats are dropped. Each customer has two orgs of its own (for example `org_hooli` and `org_initech` for Acme Labs), assigned round-robin, and test IDs include the org (`usr_hooli_0001`). Expected draft invoices: Acme Labs $3,274 (SSO 10 + 10 connections $2,500, Directory Sync $625, Radar 25,000 billable checks $50, Custom Domain $99, AuthKit covered by the free credit). Globex Corp $0 due, with $8,046 drawn from the commit. Pass the same `--suffix` you built with to target a suffixed copy.

**Archive a build**

```bash
python3 scripts/build_workos_model/archive_build.py --state-file scripts/build_workos_model/state-test2.json
python3 scripts/build_workos_model/archive_build.py --state-file scripts/build_workos_model/state-test2.json --confirm
```

Archives contracts, customers, packages, the rate card, products, then billable metrics. Without `--confirm` it only lists what it would archive. Draft invoices are cancelled. Add `--void-finalized` to also void finalized invoices.

Docs: [Packages](https://docs.metronome.com/guides/implement-metronome/core-concepts/packages-overview) · [Edit a contract](https://docs.metronome.com/api-reference/contracts/edit-a-contract) · [Design usage events](https://docs.metronome.com/guides/events/design-usage-events)

### Build the WorkOS order form contracts

Encodes three signed WorkOS order forms as contracts on the `WorkOS List Price` rate card. Run it after `build_workos_model.py`, against the same state file. Each deal is one of a kind, so each contract is created directly with its own terms instead of from a package:

1. 5 products: `WorkOS Enterprise`, `Admin Portal Custom Branding` and `Monthly Minimum` (subscriptions), `Included Connections` and `Monthly Minimum Commitment` (fixed)
2. 4 rates on the existing rate card: WorkOS Enterprise $250,000 a year, Enterprise Support $0 a year and Monthly Minimum $0 a month (placeholders each contract prices with an override), Admin Portal Custom Branding $499 a month
3. Customers and contracts:

| Customer | Term | Order form terms in Metronome |
| -------- | ---- | ----------------------------- |
| `Businessolver, Inc.` | Oct 7, 2026 to Oct 7, 2028, net 30 | Prepaid commit `Prepaid Credits` ($240K Year 1, $260K Year 2, each invoiced on its start date, full rollover to a renewal). Annual `Enterprise Support` subscription overridden to $60K on the same invoice. Tiered SSO override. Free 1M MAUs and 1,000 Radar checks as monthly quantity credits |
| `Drata Inc.` | Feb 1, 2026 to Feb 1, 2027, invoiced monthly | $30K monthly minimum as a `Monthly Minimum ($30K)` subscription, billed in arrears, that funds a linked $30K monthly credit for usage-tagged products. Unused credit expires at month end. Tiered SSO and Directory Sync overrides. `Admin Portal Custom Branding` monthly subscription |
| `ServiceNow, Inc.` | Sep 21, 2026 to Sep 21, 2028, net 30 | Annual `WorkOS Enterprise` subscription with a 0.7 multiplier ($175K a year). Included 1M MAUs, 50 SSO and 50 Directory Sync connections as monthly quantity credits named `AuthKit MAUs`, `SSO Connections` and `Directory Sync Connections` |

Ingest aliases are `team_businessolver`, `team_drata` and `team_servicenow`.

Contracts keep their signed start dates, but usage invoices start Oct 1, 2026 (`usage_statement_schedule.invoice_generation_starting_at`), so Drata isn't back-billed for February to September. Every contract bills usage on the 1st of the month so the monthly transaction IDs line up with billing periods. The cutover doesn't apply to scheduled invoices: ServiceNow's Sep 21 annual fee is still invoiced.

**Run**

```bash
python3 scripts/build_workos_model/build_order_forms.py --dry-run
python3 scripts/build_workos_model/build_order_forms.py
python3 scripts/build_workos_model/generate_test_events.py
python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_businessolver.json
python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_drata.json
python3 scripts/send_usage/send_usage.py --file scripts/build_workos_model/events_team_servicenow.json
```

IDs are added to `state.json` (or the file passed with `--state-file`), so `archive_build.py` archives the order form customers and contracts with the rest of the build. Their annual invoices are already finalized and stay in place unless you add `--void-finalized`.

Expected invoices:

- Businessolver: $300,000 annual invoice on Oct 7 (credits $240,000 + support $60,000). October usage of $16,040 (SSO 60 + 60 connections $11,250 on its tiers, Directory Sync $4,750, Radar $40) is drawn from the credits, leaving $223,960, so $0 is due.
- Drata: $499 branding fee for October on Oct 1. October usage invoice of $30,499: the $30,000 minimum subscription, $22,050 of usage (SSO 150 + 150 connections $14,700 and Directory Sync $7,350 on its tiers) fully covered by the minimum credit, and November's $499 branding fee billed in advance. Branding is billed on top of the minimum, since the credit covers only usage products.
- ServiceNow: $175,000 annual invoice on Sep 21. October usage invoice of $1,000: 30 + 30 SSO connections cost $6,750 on per-org tiers, and the 50 included connections cover $5,750 of it, leaving 10 connections at $100. MAUs and Directory Sync stay inside the included amounts.

Docs: [Create a contract](https://docs.metronome.com/api-reference/contracts/create-a-contract) · [Enterprise commit guide](https://docs.metronome.com/guides/pricing-packaging/billing-model-guides/enterprise-commit)

---



## Cursor skills

`.cursor/skills/` includes Metronome guidance from [Metronome-Industries/ai](https://github.com/Metronome-Industries/ai):


| Skill                           | Use for                                                        |
| ------------------------------- | -------------------------------------------------------------- |
| `metronome-best-practices`      | Event ingestion, contracts, invoicing, credits/commits, Stripe |
| `metronome-setup-catalog`       | Billable metrics, products, rate cards                         |
| `metronome-create-customer`     | New customer + ingest alias                                    |
| `metronome-create-contract`     | Contract from order-form terms                                 |
| `metronome-csm-reviews`         | Customer health / commit reviews                               |
| `metronome-plg-billing`         | PLG billing patterns                                           |
| `stripe-to-metronome-migration` | Stripe → Metronome migration                                   |




## Docs

- [Metronome documentation](https://docs.metronome.com/)
- [API quickstart](https://docs.metronome.com/guides/get-started/api-quickstart)
- [API reference](https://docs.metronome.com/api-reference/introduction)

