# teva-delivery: guide for coding agents

`AGENTS.md` and `CLAUDE.md` are the same file, byte for byte (`tests/unit/test_repository.py`
checks it). Edit both.

## What this repo is

The Levin Teva warehouse packing service: Python 3.15, FastAPI, SQLAlchemy 2 (sync), Alembic,
PostgreSQL in production (SQLite for demos and tests). One Hebrew, voice-controlled packing
screen (the `warehouse` module of teva-frontend) over Konimbo (the store), Cargo (the courier)
and teva-printer (labels). Start with `docs/architecture.md`; `README.md` lists the other docs.

## Layout

- `src/teva_delivery/core/` - settings (`DELIVERY_*`, `config.py`), logging, the clock.
- `src/teva_delivery/domain/` - pure rules, no I/O: the Hebrew parser (`hebrew/`), notes,
  addresses, duplicates, packing transitions, error codes with their Hebrew messages
  (`errors.py`), provider ports (`ports.py`).
- `src/teva_delivery/persistence/` - models and Alembic migrations (`migrations/versions/`).
- `src/teva_delivery/providers/` - Konimbo, Cargo, teva-printer, workspace sign-in, speech,
  Claude, and the demo providers; `factory.py` builds what the settings configure.
- `src/teva_delivery/services/` - the use cases (`work`, `packing`, `voice`, `completion`,
  `shipments`, `printing`, `jobs`, `sync`, `duplicates`, `aliases`, `overview`, `worker`) and
  `container.py`, which wires them.
- `src/teva_delivery/api/` - routers under `/api/v1` (`packing.py`, `office.py`, `events.py`),
  dependencies (sign-in), error handlers, the request-ID middleware.
- `stt/` - the separate speech recognition service (faster-whisper, Python 3.12 image).
- `deploy/` - Compose files and `runtime.env.example`. `.github/workflows/checks.yml` tests,
  builds and deploys.

## Commands

```bash
uv venv --python 3.15 && uv pip install -e ".[dev]"
python -m ruff check && python -m ruff format --check
python -m mypy src tests
python -m pytest
TEVA_TEST_POSTGRES_URL=postgresql+psycopg://user:password@host:5432/db python -m pytest -m postgres
DELIVERY_AUTH_DISABLED=true python -m teva_delivery     # demo mode on :8000, docs at /api/docs
```

A schema change needs a migration in `persistence/migrations/versions/` that works on both
SQLite and PostgreSQL.

## Rules

- **The backend decides.** Buttons, voice and the AI all become `ActionInput` and go through
  `Work.act`. The AI only proposes; `Voice._checked` validates every proposal against the order.
  Nothing writes to Konimbo or Cargo from a request handler.
- **External writes are outbox jobs** (`jobs` table) written in the same transaction as their
  cause, with a fixed idempotency key, run by the worker. A Konimbo write is read back before it
  is retried (the `[מחסן #…]` marker). A Cargo create or a label print whose answer was lost
  becomes `uncertain` and is never repeated without a person.
- **Only documented provider endpoints.** Konimbo API v1 and Cargo API v2 as `docs/konimbo.md`
  and `docs/cargo-and-labels.md` describe them. Do not add an endpoint without a source.
- **Never a label of our own.** Labels are Cargo's PDF, checked by `label_matches` against the
  shipment number before printing. A shipment number belongs to one packing unit.
- **Never merge orders automatically.** Duplicates go to the review; a phone match alone is
  never a duplicate.
- **One packer per order.** Claiming locks the row and takes a lease; picking the next order
  locks candidates one at a time with `SKIP LOCKED`. Keep the PostgreSQL concurrency test
  passing.
- **Live mode is safe by default.** `konimbo_writes` and `cargo_writes` are off until set; a job
  in test mode is recorded, not sent. Tests never call a live service.
- **Never log or expose sensitive content.** Logs carry request IDs, job and order IDs, codes and
  counts. Never customer names, phones, addresses, e-mails, tokens, cookies, label links, voice
  audio or provider error bodies. Secrets are `SecretStr`. The AI sees product names,
  quantities and masked notes only.
- **Never commit a secret.** Keys reach the service through `deploy/runtime.env` (git ignores
  it), written from the `RUNTIME_ENV` secret in CI.
- **Errors that reach a person carry a code** (`ServiceError(code)`, defined in
  `domain/errors.py` with its Hebrew message). The API answers `{"detail": code, "message"}`.
- **Time comes from the clock** (`context.now()`, `ManualClock` in tests), never
  `datetime.now()` in services.
- **Hebrew user text, English code and docs.** Messages on the screen and spoken answers are
  Hebrew; identifiers, comments and documentation are English.

## Tests

`tests/` uses pytest with SQLite in a temporary directory, a `ManualClock`, the demo providers
and the fakes in `tests/fakes.py`; `tests/support.py` has the helpers (`build`, `act`,
`pack_everything`, `settle`). The worker is run by hand (`settle`) instead of its thread.
Providers are tested with `httpx.MockTransport` (`httpx2.MockTransport` for the Anthropic SDK).

| Change | Folder |
| --- | --- |
| Hebrew parser, domain rules, settings, wiring | `tests/unit/` |
| Konimbo, Cargo, teva-printer, speech, sign-in and AI clients | `tests/providers/` |
| Packing, voice, shipments, jobs, sync, duplicates | `tests/services/` |
| HTTP endpoints and the WebSocket | `tests/api/` |
| PostgreSQL (locking, concurrency, migrations) | `tests/integration/` (marked `postgres`) |

Coverage must stay at or above `fail_under` in `pyproject.toml`.
