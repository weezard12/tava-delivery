"""Service settings, read from DELIVERY_* environment variables.

Deployments write them from the RUNTIME_ENV secret into deploy/runtime.env, which Compose
passes to the container. Local development can use a .env file. Secrets are SecretStr, so
they never appear in a repr, a validation error or a log line.
"""

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from ..domain.enums import Category, ShippingProfile

_SERVICE_KEY = re.compile(r"[!-~]{32,512}")
MIN_SYNC_INTERVAL = 30


class ShippingRule(BaseModel):
    """Maps an order to a shipping profile. The first matching rule wins.

    ``match`` is a piece of the Konimbo shipping method title (case and spacing ignored);
    ``category`` limits the rule to orders of one warehouse category. A rule with neither
    matches every order, so it belongs last.
    """

    match: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    profile: ShippingProfile


DEFAULT_SHIPPING_RULES = (
    ShippingRule(match="איסוף עצמי", profile=ShippingProfile.NONE),
    ShippingRule(category=Category.PICKUP_POINTS, profile=ShippingProfile.BOX),
    ShippingRule(profile=ShippingProfile.EXPRESS),
)


def split_list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DELIVERY_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    # demo: synthetic orders, fake Cargo and printer, nothing leaves the process.
    # live: the real Konimbo store, Cargo and teva-printer, each only when configured.
    mode: Literal["demo", "live"] = "demo"
    data_dir: Path = Path("data")
    timezone: str = "Asia/Jerusalem"

    # Database. PostgreSQL in production; without it the service keeps a SQLite file in
    # data_dir, which is only meant for demos and local development.
    postgres_host: str = ""
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_database: str = ""
    postgres_user: str = ""
    postgres_password: SecretStr = SecretStr("")
    postgres_sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"

    # Shared workspace sign-in (teva-auth). The service key equals TEVA_AUTH_DELIVERY_KEY.
    auth_url: AnyHttpUrl | None = None
    auth_service_key: SecretStr = SecretStr("")
    auth_cookie_name: Literal["__Host-teva_session", "teva_dev_session"] = "__Host-teva_session"
    # Demo mode only: accept requests without a workspace session, as "demo-user" or the
    # account named in an X-Demo-User header. For local development and browser tests.
    auth_disabled: bool = False

    # Konimbo, the store. Konimbo stays the source of every commercial fact.
    konimbo_token: SecretStr = SecretStr("")
    konimbo_writes: bool = False
    konimbo_status_awaiting_inventory: str = "ממתין למלאי"
    konimbo_status_consolidation: str = "איחוד הזמנות"
    konimbo_status_urgent: str = "מיוחדים דחופים"
    konimbo_status_pickup_points: str = "נקודות איסוף"
    # Statuses of new orders that are ready for the warehouse, comma separated.
    konimbo_status_regular: str = "הזמנה חדשה"
    # Optional: set when packing starts, so the office sees that the warehouse has the order.
    konimbo_status_preparing: str = ""
    # The status whose change makes Konimbo's Cargo integration send the order to Cargo, and
    # when the service sets it: when packing starts, when packing ends, or never (the office
    # does it). It may be the awaiting pickup status itself.
    konimbo_status_transmit: str = ""
    transmit_on: Literal["claim", "complete", "off"] = "complete"
    konimbo_status_awaiting_pickup: str = "ממתין לאיסוף"
    # A status title that is NOT one of the store's order states, so appending it only adds a
    # line to the order history. Warehouse notes are written as its comment.
    konimbo_note_title: str = "הערת מחסן"
    konimbo_status_username: str = Field(default="מחסן", min_length=1, max_length=60)
    # Payment statuses that let an order be packed. Empty: payment is shown but not checked.
    konimbo_paid_statuses: str = ""
    # Payment statuses that mean the payment failed. Such orders never enter the queue.
    konimbo_failed_payment_statuses: str = ""
    import_days: int = Field(default=21, ge=1, le=120)
    sync_interval_seconds: float = Field(default=120, ge=0, le=3600)
    # Requests per 10 minutes this service may spend. Konimbo allows 100 per token.
    konimbo_request_budget: int = Field(default=70, ge=5, le=100)
    product_images: bool = True
    product_image_fetches_per_sync: int = Field(default=10, ge=0, le=50)

    # Cargo, the courier (API v2, the one Cargo's own plugins use).
    cargo_token: SecretStr = SecretStr("")
    cargo_writes: bool = False
    cargo_customer_code_express: int | None = Field(default=None, ge=1)
    cargo_customer_code_box: int | None = Field(default=None, ge=1)
    cargo_from_name: str = "לוין טבע"
    cargo_from_street: str = ""
    cargo_from_street_number: str = ""
    cargo_from_city: str = ""
    cargo_from_phone: str = ""
    cargo_website: str = "https://www.levinteva.co.il"
    cargo_status_sync_minutes: float = Field(default=20, ge=0, le=1440)
    # konimbo: Konimbo's Cargo integration creates the shipment when the order's status
    # changes, and the service reads the shipment number back from the order. cargo: the
    # service creates the shipment through Cargo's API itself.
    shipment_source: Literal["konimbo", "cargo"] = "konimbo"
    shipment_number_pattern: str = (
        r"(?i)(?:cargo|קארגו|קרגו|משלוח|shipment|tracking|מעקב)\D{0,30}?(\d{6,12})"
    )
    shipping_rules: tuple[ShippingRule, ...] = DEFAULT_SHIPPING_RULES

    # Label printing through teva-printer.
    printer_url: AnyHttpUrl | None = None
    # Standalone teva-printer only (PRINTER_TOKEN). In the workspace the employee's own session
    # is passed on with each print, so the printer records who printed.
    printer_token: SecretStr = SecretStr("")
    label_printer_id: str = Field(default="", max_length=128)
    print_direct: bool = True
    auto_print: bool = False
    require_label_print: bool = True

    # Voice. Speech is recognised by the local STT service; the AI interpreter is optional.
    stt_url: AnyHttpUrl | None = None
    stt_token: SecretStr = SecretStr("")
    stt_min_confidence: float = Field(default=0.45, ge=0, le=1)
    voice_duplicate_seconds: float = Field(default=4, ge=0, le=60)
    voice_transcript_days: int = Field(default=30, ge=1, le=365)
    ai_provider: Literal["none", "anthropic"] = "none"
    anthropic_api_key: SecretStr = SecretStr("")
    # The model that resolves product nicknames the rules could not. A smaller model such as
    # claude-haiku-5-5 answers faster and costs less.
    ai_model: str = Field(default="claude-opus-5-5", min_length=1, max_length=100)

    # Packing.
    lease_seconds: int = Field(default=180, ge=30, le=3600)
    worker_enabled: bool = True
    worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    job_max_attempts: int = Field(default=6, ge=1, le=50)

    log_format: Literal["text", "json"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator(
        "auth_url",
        "printer_url",
        "stt_url",
        "cargo_customer_code_express",
        "cargo_customer_code_box",
        mode="before",
    )
    @classmethod
    def allow_blank(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("shipping_rules", mode="before")
    @classmethod
    def parse_rules(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            return DEFAULT_SHIPPING_RULES if not text else json.loads(text)
        return value

    @field_validator("log_format", "log_level", mode="before")
    @classmethod
    def ignore_case(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip()
        return text.lower() if text.lower() in ("text", "json") else text.upper()

    @model_validator(mode="after")
    def validate_configuration(self) -> Settings:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("DELIVERY_TIMEZONE must be an IANA time zone.") from None
        if 0 < self.sync_interval_seconds < MIN_SYNC_INTERVAL:
            raise ValueError(
                f"DELIVERY_SYNC_INTERVAL_SECONDS must be 0 or at least {MIN_SYNC_INTERVAL}."
            )
        key = self.auth_service_key.get_secret_value()
        if self.auth_url is None:
            if key:
                raise ValueError("DELIVERY_AUTH_SERVICE_KEY requires DELIVERY_AUTH_URL.")
        else:
            url = self.auth_url
            if url.path not in (None, "/") or any(
                part is not None for part in (url.username, url.password, url.query, url.fragment)
            ):
                raise ValueError("DELIVERY_AUTH_URL must be the workspace auth service origin.")
            if not _SERVICE_KEY.fullmatch(key):
                raise ValueError(
                    "DELIVERY_AUTH_SERVICE_KEY requires 32 to 512 printable characters."
                )
        if self.auth_disabled and self.mode != "demo":
            raise ValueError("DELIVERY_AUTH_DISABLED is only allowed in demo mode.")
        if self.mode == "live":
            if self.auth_url is None:
                raise ValueError("Live mode requires workspace sign-in (DELIVERY_AUTH_URL).")
            if not self.postgres_configured:
                raise ValueError(
                    "Live mode requires PostgreSQL: set DELIVERY_POSTGRES_HOST, _DATABASE, "
                    "_USER and _PASSWORD."
                )
        postgres = (
            self.postgres_host,
            self.postgres_database,
            self.postgres_user,
            self.postgres_password.get_secret_value(),
        )
        if any(postgres) and not all(postgres):
            raise ValueError("Set DELIVERY_POSTGRES_HOST, _DATABASE, _USER and _PASSWORD together.")
        if self.ai_provider == "anthropic" and not self.anthropic_api_key.get_secret_value():
            raise ValueError("DELIVERY_AI_PROVIDER=anthropic requires DELIVERY_ANTHROPIC_API_KEY.")
        return self

    @property
    def postgres_configured(self) -> bool:
        return bool(self.postgres_host and self.postgres_database and self.postgres_user)

    @property
    def database_url(self) -> URL | Path:
        if self.postgres_configured:
            return URL.create(
                "postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password.get_secret_value(),
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_database,
                query={"sslmode": self.postgres_sslmode},
            )
        return self.data_dir / "delivery.sqlite3"

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def konimbo_configured(self) -> bool:
        return bool(self.konimbo_token.get_secret_value())

    @property
    def cargo_configured(self) -> bool:
        return bool(self.cargo_token.get_secret_value()) and (
            self.cargo_customer_code_express is not None or self.cargo_customer_code_box is not None
        )

    @property
    def cargo_sender_complete(self) -> bool:
        return all(
            (
                self.cargo_from_name,
                self.cargo_from_street,
                self.cargo_from_city,
                self.cargo_from_phone,
            )
        )
