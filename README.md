# Metronome Solution Architect onboarding scripts

A collection of scripts for Solution Architects working with [Metronome](https://docs.metronome.com/). Shared auth lives in `.env`.

## Layout

```
.env                         # API token and shared defaults (gitignored)
scripts/send_usage/          # Send usage events
  send_usage.py
  events.json
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

