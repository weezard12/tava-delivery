# Konimbo

Konimbo is the store and the source of every commercial fact: the order, its lines, prices,
payment status, shipping method and notes. The warehouse keeps only what packing needs and
its own progress. Everything here comes from Konimbo's published API v1 documentation
(`https://api.konimbo.co.il/v1`); no undocumented endpoint is used.

## Calls

| Call | Used for |
| --- | --- |
| `GET /orders?status_option_title=…&created_at_min=…&attributes=…&page=…` | The import: one listing per warehouse status, newest first, paged by the `X-Pagination-Links` header. |
| `GET /orders/{id}?attributes=…` | Reading one order back: before a retried write, when looking for the Cargo shipment number. |
| `PUT /orders/{id}` with `{"order": {"statuses": [{"status_option_title", "username", "comment"}]}}` | Appending a status line. |
| `GET /items/{id}?attributes=id,title,images` | A product picture, a few per sync, cached. |

The token travels as the `token` query parameter, as Konimbo documents, so httpx's own request
log is turned off and the service never logs URLs. `attributes` always names the fields the
warehouse needs (`id, payment_status, name, email, phone, address, note, total_price,
status_option_title, created_at, items, upgrades, shipping, statuses`). Card details are
never requested.

## Statuses

Konimbo has no endpoint that lists a store's status options. A status `PUT` with a title the
store knows changes the order's state; any other title only adds a line to its history.

- **Categories are statuses.** The warehouse reads orders in five statuses: awaiting inventory,
  consolidation, urgent, pickup points and the regular "new order" statuses. Their exact names
  are settings, changed at runtime on the settings screen (admin). The screen offers the names
  the service has seen on imported orders, and *Learn the store's statuses* reads recent orders
  to find more (one or a few requests).
- **What the warehouse writes.**
  - *Preparing* (optional): when packing starts, so the office sees the warehouse has it.
  - *Transmit*: the status whose change makes Konimbo's Cargo integration send the order to
    Cargo, set on claim, on completion, or never (see [cargo-and-labels.md](cargo-and-labels.md)).
  - *Awaiting pickup* (`ממתין לאיסוף`): once the label is printed. The order is completed when
    the next import sees this status.
  - *Awaiting inventory* (`ממתין למלאי`): when an employee reports missing stock and leaves the
    order.
  - *Warehouse note* (`הערת מחסן`): a title that must **not** be one of the store's statuses,
    so appending it only adds a line to the history. Skip reasons and missing items are written
    as its comment.
- Every comment the service writes ends with a short tag made from the job's key (`[מחסן #…]`). A retried write first
  reads the order and looks for the marker, so a write whose answer was lost is not repeated.
- Writes to one order run in order, one at a time.

## The import

Every `sync_interval_seconds` (default 120) the worker reads each warehouse status, for orders
created in the last `import_days` (default 21):

- A new order becomes a fulfillment in its category, with its lines and its notes: the
  customer's order note and the comments the office wrote in the order history, each split
  into packing notes (to confirm) and delivery notes (for the courier).
- A known order is updated from Konimbo. Packing progress and confirmations stay here. A line
  that changed while being packed is flagged for the employee.
- If the office moves an order between categories, the queue follows. If it moves it to
  awaiting inventory, the order leaves the queue; moving it back means the stock arrived.
- An order that left every warehouse status before packing started leaves the queue; one that
  is being packed is flagged for the employee instead.
- A failed payment keeps an order out of the queue. With `konimbo_paid_statuses` set, an order
  whose payment status is not in the list goes to review.
- Duplicate orders (same customer, same items, close in time) are grouped and wait for a
  person to decide; nothing is merged automatically, and a matching phone alone is never
  enough.

## Rate budget

Konimbo allows 100 requests per 10 minutes per token. The client keeps its own sliding window
(`konimbo_request_budget`, default 70), reads Konimbo's rate headers when present, and stops
for the rest of the window on a 429. Product pictures are fetched only while at least 10
requests are left. The import status on the screen shows the requests left.

## Test mode

Until `TEVA_KONIMBO_WRITES=true`, live mode reads Konimbo but writes nothing: each status
or note job is recorded as *test mode* and finished without a request, and the screen says so.
