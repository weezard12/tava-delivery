# Architecture

```
teva-frontend (warehouse module) ──/services/delivery/──▶ teva-delivery (FastAPI, one process)
                                                            │  ├─ API + WebSocket events
                                                            │  └─ worker thread: outbox jobs, import, status sync
                                                            ├─▶ PostgreSQL (state, outbox, audit)
                                                            ├─▶ Konimbo API v1 (orders, statuses, product pictures)
                                                            ├─▶ Cargo API v2 (label links; shipments in the cargo source)
                                                            ├─▶ teva-printer (Cargo labels, the employee's session passed on)
                                                            ├─▶ stt (local faster-whisper, Hebrew)
                                                            └─▶ Claude (optional, product names only)
```

## Layers

`src/teva_delivery/` follows teva-printer's layout:

- `core/`: settings (`DELIVERY_*`), logging, the clock.
- `domain/`: pure rules with no I/O. The Hebrew parser (`hebrew/`), note classification,
  address splitting, duplicate grouping, packing rules and state transitions, error codes and
  their Hebrew messages, and the provider ports.
- `persistence/`: SQLAlchemy models and Alembic migrations (applied on start under an advisory
  lock on PostgreSQL).
- `providers/`: Konimbo, Cargo, teva-printer, workspace sign-in, speech, Claude, and the demo
  versions. A provider that is not configured is `None`, and the service says so on the screen.
- `services/`: the use cases. `work` (queue, claiming, navigation), `packing` (actions),
  `voice`, `completion` (what happens after packing), `shipments`, `printing`, `jobs` (Konimbo
  writes and finding the shipment number), `sync` (the import), `duplicates`, `aliases`,
  `overview` (readiness, settings), `worker` (the outbox runner).
- `api/`: FastAPI routers, the request-ID middleware and the error handlers.

## A packing unit

The service packs a *fulfillment*: one Konimbo order, or several a person chose to consolidate.

```
READY ─claim─▶ PACKING ─complete─▶ PACKED ─store confirms pickup status─▶ COMPLETED
  ▲               │  │                └─reopen (before the pickup status is sent)─▶ PACKING
  └─release/next──┘  └─missing stock + leave─▶ AWAITING_INVENTORY ─stock arrived─▶ READY
REVIEW (duplicate or unverified payment) ─person decides─▶ READY or CANCELLED
```

Progress is an event log (`packing_events`), so undo is "reverse the last confirmation" and
the history of every order is kept.

## Never twice, never the wrong order

- **One packer per order.** Claiming takes a row lock and a lease (`lease_seconds`, renewed by
  the screen's heartbeat). Picking the next order ranks candidates without locking and then
  locks one at a time with `SKIP LOCKED`, so packers asking at the same moment get different
  orders (tested on PostgreSQL).
- **Idempotent actions.** Each button press and voice command carries a key; the same key
  returns the stored answer, a different body with the same key is refused.
- **Outbox jobs.** A Konimbo status, a warehouse note or a Cargo shipment request is a row in
  `jobs`, written in the same transaction as its cause and run by the worker. Writes to one
  Konimbo order run in order.
- **Read back before writing again.** A Konimbo write is marked as sent before it is sent;
  if the answer is lost, the next attempt reads the order and looks for the job's marker
  (`[מחסן #…]`) before writing. A Cargo create that got no answer becomes *uncertain* and is
  never retried by itself: a person checks Cargo and links the number or confirms it was not
  created.
- **Labels.** The label link must be Cargo's (`https://api-v2.cargo.co.il/labels/…`) and name
  the shipment linked to this order (`shipment_<number>_…pdf`), or nothing is printed. A
  shipment number belongs to one order only. A print that may have happened is never repeated
  without an explicit reprint.

## Live updates

Services publish `{"type": "fulfillment", "id", "version"}` and `{"type": "queue"}` after
their transaction commits. `/api/v1/events` (WebSocket) forwards them; the screen refetches.

## Privacy

Logs carry request IDs, job and order IDs, codes and counts: never customer names, phones,
addresses, tokens, cookies or label links (httpx's own request log is silenced because
Konimbo's token travels in the URL). Phones and e-mails are masked in the duplicate review.
The AI sees product names, quantities and masked packing notes, never contact details.
