# Cargo shipments and labels

Cargo is the courier. Every label the warehouse prints is Cargo's own PDF; this service never
draws a label, a barcode or a replacement for one.

The Cargo calls come from Cargo API v2 as Cargo's own WooCommerce plugin uses it
(`https://api-v2.cargo.co.il/api/`, a Bearer token):

| Call | Used for |
| --- | --- |
| `POST shipments/print-label` | The label link of a shipment. |
| `POST shipments/get-status` | Checking a number before it is linked, and following delivery. |
| `POST shipments/create` | Creating a shipment, with the `cargo` source only. |

Cargo has no idempotency key and no lookup by order number, so preventing a duplicate shipment
is this service's job.

## Where the shipment comes from

`DELIVERY_SHIPMENT_SOURCE` (or the settings screen) picks one of two ways.

### `konimbo` (the default)

Konimbo's own Cargo integration sends an order to Cargo when the order's status changes. The
service does not create the shipment; it sets that status and reads the number back.

1. **Transmit.** At the moment `transmit_on` names, the service appends the *transmit* status
   (`konimbo_status_transmit`) to the primary order:
   - `claim`: when packing starts, so the label is ready by the end of packing;
   - `complete` (the default): when everything is packed and confirmed;
   - `off`: never; the office sets it in Konimbo.

   The transmit status may be the awaiting pickup status itself. Then the shipment can only
   exist after completion, so completion does not wait for a label and the label is printed
   afterwards. Orders packed together are sent once, as the primary order.
2. **Find the number.** A `konimbo.find_shipment` job reads the order 20 seconds later, and
   again with growing gaps (up to 5 minutes, 20 tries). It looks at Konimbo's own history
   comments written since the transmit (never the service's own) and any tracking fields,
   with `shipment_number_pattern`, a regular expression the admin can change.
   - Exactly one new number: checked with Cargo when Cargo is connected (a cancelled shipment
     is refused) and attached.
   - Several numbers: nothing is guessed. The screen asks a person to type the right one.
   - Nothing after every try: the screen says so and offers the same manual link.
3. **Link by hand.** *Link a shipment* takes a number a person found in Konimbo or Cargo,
   checks it with Cargo and attaches it.

### `cargo`

The service creates the shipment itself through `shipments/create`, from the order's
address, phone and the shipping profile's Cargo customer code, once `DELIVERY_CARGO_WRITES`
is on. Each attempt is a row written before the request leaves and sent at most once. An
attempt whose answer never came becomes *uncertain* and blocks any new attempt until a person
checks Cargo and either links the shipment that was made or confirms that none was
(*not created*, admin only). Batch creation is offered for the office.

### Shipping profiles

`shipping_rules` map an order to a profile by its Konimbo shipping method and its category:

- `none`: no courier (self pickup, `איסוף עצמי`). No shipment, no label.
- `box`: a pickup point (`cargo_customer_code_box`).
- `express`: home delivery (`cargo_customer_code_express`).

The first matching rule wins; the default rules are self pickup, then pickup points, then
everything else express.

## A number belongs to one order

A shipment number is attached to one packing unit only. A number another order holds is
refused, whether it came from Konimbo, from Cargo or from a person, and so are the order's own
number and numbers of shipments cancelled here.

## Printing the label

1. The label link is asked from Cargo for the attached shipment.
2. The link must be Cargo's (`https://api-v2.cargo.co.il/labels/…`) and its file must name this
   shipment (`shipment_<number>_….pdf`). Otherwise nothing is printed and the screen shows
   *label_mismatch*.
3. The link goes to teva-printer's `POST /api/v1/printers/{id}/cargo-labels` with the
   employee's own workspace session, so the printer records who printed. teva-printer fetches
   the PDF from Cargo itself.
4. The print is recorded as *uncertain* before it is sent. Accepted becomes *accepted*; a
   clear refusal becomes *failed*; a lost answer stays *uncertain*.

Rules:

- A label is printed once. A second print needs an explicit **reprint**.
- An uncertain print is never repeated by itself: a person looks at the printer and chooses
  reprint.
- With `auto_print`, the packer's screen prints the label by itself once, when the order is
  packed and its shipment is known.
- With `require_label_print` (the default) completion waits for an accepted label print,
  unless labels cannot be printed here at all (no Cargo token or no label printer).

## After the label

When the label is printed (or not required), the service appends *awaiting pickup*
(`ממתין לאיסוף`) with the shipment number in the comment. The order is completed when the
next import sees that status. Every `cargo_status_sync_minutes` the worker asks Cargo for the
status of open shipments (20 at a time); a shipment cancelled in Cargo is flagged on its order.
