# Open questions

What still needs an answer from the business or from the providers, and what the service does
until then. Each one is a setting, so the answer needs no code change.

## Which status sends the order to Cargo

Konimbo's Cargo integration sends an order when its status changes, but which status triggers
it is set in Konimbo. The service needs its exact name (`konimbo_status_transmit`) and when to
set it (`transmit_on`):

- If it is a status of its own ("נשלח ל-Cargo" or similar), set it on `claim` so the label is
  ready when packing ends, or on `complete`.
- If it is awaiting pickup (`ממתין לאיסוף`) itself, the shipment exists only after completion,
  so the label is printed after the order is marked; set the transmit status to the same name.
- Until it is set, nothing is sent to Cargo by the service, and a shipment number can be
  linked by hand.

## Where Konimbo shows the Cargo shipment number

The service reads the number from the order's history comments and tracking fields, with
`shipment_number_pattern`. Konimbo's API documents no tracking field, so this needs one real
order sent to Cargo, read with the API, to see where the number appears and adjust the pattern.
Until then a person links the number from Cargo by hand.

## Pickup points

Cargo's BOX service (pickup points) needs the pickup point the customer chose, and Konimbo's
order API does not say which one it is. With the default `konimbo` source Konimbo's
integration sends it. With the `cargo` source the service refuses to create a pickup point
shipment (`pickup_point_unknown`) and a person links the number instead, until the point can
be read from the order.

## Keys and access

- Konimbo API token (`DELIVERY_KONIMBO_TOKEN`).
- Cargo API token and customer codes (`DELIVERY_CARGO_TOKEN`, `_CUSTOMER_CODE_EXPRESS`, `_BOX`).
- The workspace sign-in key (`TEVA_AUTH_DELIVERY_KEY` in teva-media-master, the same value as
  `DELIVERY_AUTH_SERVICE_KEY`).
- Optional: an Anthropic API key for the AI fallback.

## To confirm with the warehouse

- The exact names of the statuses (learned from the store on the settings screen).
- The label printer's ID in teva-printer.
- The payment statuses that allow packing, if payment should be checked.
- Whether a skipped order should go back to the queue automatically or only by hand (today:
  by hand, from the queue screen).
