# Operations

## Modes

- **demo** (the default): invented orders in an in-memory store, a fake Cargo and a fake
  printer, and a simulation of Konimbo's Cargo integration. Nothing leaves the process.
  SQLite in `DELIVERY_DATA_DIR` unless PostgreSQL is configured. `DELIVERY_AUTH_DISABLED=true`
  is allowed here only, for local work and browser tests.
- **live**: the real Konimbo store, Cargo, teva-printer, speech and AI, each only once it is
  configured; whatever is missing is shown on the screen (`GET /api/v1/info`). Requires the
  workspace sign-in and PostgreSQL. Konimbo and Cargo writes stay off until each is enabled.

## Settings

Environment settings start with `DELIVERY_` (`core/config.py` documents each one). Secrets are
never logged and never shown on the screen.

| Group | Settings |
| --- | --- |
| Service | `MODE`, `DATA_DIR`, `TIMEZONE` (Asia/Jerusalem), `LOG_FORMAT` (`json` in deployments), `LOG_LEVEL` |
| Database | `POSTGRES_HOST`, `_PORT`, `_DATABASE`, `_USER`, `_PASSWORD`, `_SSLMODE` |
| Sign-in | `AUTH_URL` (the workspace auth service), `AUTH_SERVICE_KEY` (= `TEVA_AUTH_DELIVERY_KEY` there), `AUTH_COOKIE_NAME` |
| Konimbo | `KONIMBO_TOKEN`, `KONIMBO_WRITES`, `KONIMBO_STATUS_*`, `TRANSMIT_ON`, `KONIMBO_NOTE_TITLE`, `KONIMBO_STATUS_USERNAME`, `KONIMBO_PAID_STATUSES`, `KONIMBO_FAILED_PAYMENT_STATUSES`, `IMPORT_DAYS`, `SYNC_INTERVAL_SECONDS`, `KONIMBO_REQUEST_BUDGET`, `PRODUCT_IMAGES` |
| Cargo | `CARGO_TOKEN`, `CARGO_WRITES`, `CARGO_CUSTOMER_CODE_EXPRESS`, `_BOX`, `CARGO_FROM_*`, `SHIPMENT_SOURCE`, `SHIPMENT_NUMBER_PATTERN`, `SHIPPING_RULES` (JSON), `CARGO_STATUS_SYNC_MINUTES` |
| Labels | `PRINTER_URL`, `PRINTER_TOKEN` (standalone teva-printer only), `LABEL_PRINTER_ID`, `PRINT_DIRECT`, `AUTO_PRINT`, `REQUIRE_LABEL_PRINT` |
| Voice | `STT_URL`, `STT_TOKEN`, `STT_MIN_CONFIDENCE`, `VOICE_DUPLICATE_SECONDS`, `VOICE_TRANSCRIPT_DAYS`, `AI_PROVIDER`, `ANTHROPIC_API_KEY`, `AI_MODEL` |
| Packing | `LEASE_SECONDS`, `WORKER_ENABLED`, `WORKER_POLL_SECONDS`, `JOB_MAX_ATTEMPTS` |

**The settings screen.** Status names, the transmit status and moment, payment statuses, the
shipment source and number pattern, shipping rules, the label printer and the print rules are
also stored in the database and changed at runtime by an admin (`PATCH /api/v1/settings`). The
environment gives their first values; a change on the screen wins and is audited. The screen
offers the Konimbo status names seen so far and the printers teva-printer knows.

## Deployment

`.github/workflows/checks.yml` runs on pull requests and pushes to `main`, `master`, `dev` and
`develop`: lint, types, tests (SQLite and PostgreSQL), the Docker image in demo and in live
mode, and the speech image. A push to `main` deploys to the **production** GitHub environment
in live mode; a push to `dev` deploys to **staging** in demo mode. *Run workflow* with
*Deploy* does the same for the chosen branch. The first push that creates a branch does not
deploy.

The deploy job reaches the Docker server over SSH through Cloudflare Access, like teva-printer,
writes the `RUNTIME_ENV` secret to `deploy/runtime.env` and runs Compose:

- `deploy/compose.yml`: the service, as `teva-delivery:8000` on the workspace network
  (`teva-<environment>`), data in the `delivery-data` volume.
- `deploy/compose.database.yml`, added when `DELIVERY_POSTGRES_HOST` is set: joins the
  database network (`TEVA_DB_NETWORK`, default `teva-db`).
- `deploy/compose.stt.yml`, added when `DELIVERY_STT_URL` is set: the speech service as
  `teva-delivery-stt:9000`. The first start downloads the model (about 1.6 GB) into the
  `stt-models` volume; until then voice falls back to the browser. Only
  `DELIVERY_STT_TOKEN` is passed to it, as `STT_TOKEN`.

GitHub environment secrets: `RUNTIME_ENV`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`,
`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, and optionally `DEPLOY_USER` and
`DEPLOY_PORT`. Variable: `TEVA_DB_NETWORK` if the database network is not `teva-db`.
`deploy/runtime.env.example` lists the lines `RUNTIME_ENV` holds.

The service applies its migrations on start (Alembic, under an advisory lock on PostgreSQL).
`/healthz` checks the database and is the container's health check; `/livez` only says the
process answers.

## Going live, step by step

Each step can be checked on the screen before the next one.

1. **Database.** On the PostgreSQL server (`teva-postgres-postgres-1` on `teva-db`), create
   a `delivery` user and database. Put `DELIVERY_POSTGRES_*` in `RUNTIME_ENV`.
2. **Sign-in.** Generate a key of at least 32 characters. Set it as `TEVA_AUTH_DELIVERY_KEY`
   in teva-media-master's runtime settings and as `DELIVERY_AUTH_SERVICE_KEY` here, with
   `DELIVERY_AUTH_URL=http://teva-auth:8002`.
3. **Frontend.** Deploy teva-frontend with the warehouse module; its nginx sends
   `/services/delivery/` to `teva-delivery:8000`, WebSocket included.
4. **Deploy in live mode** with only the above. The screen opens with an empty queue and says
   Konimbo, Cargo and the printer are not connected.
5. **Konimbo, read only.** Add `DELIVERY_KONIMBO_TOKEN`; keep `DELIVERY_KONIMBO_WRITES=false`.
   On the settings screen press *Learn the store's statuses* and choose the exact names of the
   five categories, awaiting pickup, the warehouse note title (one that is not a store
   status) and the transmit status. Check that the queue shows the right orders. Packing works;
   every Konimbo write is recorded as *test mode*.
6. **Cargo labels.** Add `DELIVERY_CARGO_TOKEN` and the customer codes. Set
   `DELIVERY_PRINTER_URL=http://teva-printer:8000` and choose the label printer on the settings
   screen. Link a real shipment by hand on a test order and print its label.
7. **Writes.** Set `DELIVERY_KONIMBO_WRITES=true`. Pack one real order end to end and check in
   Konimbo that the transmit status, the Cargo shipment and awaiting pickup appear. Only with
   the `cargo` shipment source: `DELIVERY_CARGO_WRITES=true`.
8. **Voice.** Set `DELIVERY_STT_URL=http://teva-delivery-stt:9000` and a `DELIVERY_STT_TOKEN`;
   optionally `DELIVERY_AI_PROVIDER=anthropic` with `DELIVERY_ANTHROPIC_API_KEY`.

## Day to day

- **Jobs.** A Konimbo write or a shipment lookup that failed is shown on its order; an admin
  can retry it (`POST /api/v1/jobs/{id}/retry`) after checking the order in Konimbo.
- **Uncertain things.** An uncertain Cargo shipment or label print is never repeated by itself.
  Check Cargo or the printer, then link the shipment, confirm it was not created, or reprint.
- **Import.** `GET /api/v1/sync` shows the last runs and the Konimbo requests left;
  `POST /api/v1/sync/run` runs one now (admin).
- **Retention.** Voice transcripts are deleted after `voice_transcript_days`; stored action
  answers after two days. Orders, events and the audit log are kept.
- **Logs** are JSON lines with a request ID, never customer details or secrets.
