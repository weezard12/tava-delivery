# Teva Delivery

The Levin Teva warehouse packing system: one Hebrew, voice-controlled screen where an employee
packs an order from Konimbo, confirms every product and packing note, and finishes with the
Cargo label printed and the order marked as waiting for pickup.

The backend (this repository) is the authority. Buttons and voice commands both become the
same validated actions; the optional AI only helps the Hebrew rules name a product, and its
proposals are checked like any other request. Konimbo stays the source of every commercial
fact, Cargo is the courier, and labels print through `teva-printer`. The screen itself is the
`warehouse` module of `teva-frontend`.

## What it does

- **Queue.** Imports orders from Konimbo by status: consolidation, urgent, pickup points and
  regular orders, in that order. Orders waiting for stock and orders a person must review stay
  out of the queue. Status names are settings an admin changes at runtime, with the names
  learned from the store offered as options.
- **Packing.** Big product rows with pictures and quantities, coloured by state (neutral,
  orange, green, red), packing notes that must be confirmed, delivery notes for the courier,
  progress, and navigation: next, previous, back to an unfinished order, undo, pause, skip with
  a reason, report missing stock, complete. One order is packed by one employee at a time.
- **Voice.** "שמתי סרום שפע", "שמתי שתי משחות", "מה עוד חסר?", "חסר לנו סבון", "תבטל את
  המוצר האחרון", "תדפיס מדבקה", "עבור להזמנה הבאה". Speech is recognised by a local service;
  a deterministic Hebrew parser turns it into actions; an ambiguous product is asked about.
- **Completion.** Konimbo's Cargo integration sends the order to Cargo when its status changes,
  so by default the service sets that status and reads the shipment number back from the
  order. The label comes from Cargo and is never generated here. Then the order is marked as
  waiting for pickup. A finished order is never shipped or printed again without an explicit
  reprint.
- **Safety.** Every external write is an outbox job, recorded with its cause and retried with
  a read-back first, so nothing is written twice. Duplicate orders are grouped and shown to a
  person; nothing is merged automatically. Live mode writes nothing to Konimbo or Cargo until
  each is enabled. Logs never hold customer details, tokens or label links.

## Run it

```bash
uv venv --python 3.15 && uv pip install -e ".[dev]"
TEVA_AUTH_DISABLED=true python -m teva_delivery          # demo mode, http://localhost:8000
python -m pytest                                              # SQLite
TEVA_TEST_POSTGRES_URL=postgresql+psycopg://user@host/db python -m pytest -m postgres
python -m ruff check && python -m ruff format --check && python -m mypy src tests
```

Demo mode serves invented orders from an in-memory store, a fake Cargo and a fake printer,
and plays Konimbo's Cargo integration, so the whole flow runs with nothing leaving the
process. The API is documented at `/api/docs`.

## Documentation

- [docs/architecture.md](docs/architecture.md): layers, states, jobs, and how double work is prevented.
- [docs/konimbo.md](docs/konimbo.md): the Konimbo API calls used, statuses and the import.
- [docs/cargo-and-labels.md](docs/cargo-and-labels.md): shipments, the shipment number, labels.
- [docs/voice.md](docs/voice.md): speech recognition, the Hebrew parser and the AI fallback.
- [docs/operations.md](docs/operations.md): settings, deployment, going live step by step.
- [docs/open-questions.md](docs/open-questions.md): what still needs an answer.
