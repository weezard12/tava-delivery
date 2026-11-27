"""Shipments, inventory, duplicate review, aliases, settings and the sync status."""

from typing import Any

from fastapi import APIRouter, Response

from ...services.overview import SettingsView
from ...services.views import (
    AliasView,
    DuplicateReviewView,
    FulfillmentView,
    InfoView,
    SyncRunView,
    SyncView,
)
from ..dependencies import Actor, Admin, Credentials, ServicesDep
from .schemas import AliasBody, BatchBody, DecisionBody, LinkShipmentBody, SettingsBody

router = APIRouter()


@router.get("/info")
def info(services: ServicesDep, actor: Actor) -> InfoView:
    """What is connected, what is missing, and the names the screen shows."""
    return services.overview.info()


# Shipments.


@router.post("/fulfillments/{fulfillment_id}/shipment/link")
def link_shipment(
    services: ServicesDep, actor: Actor, fulfillment_id: int, body: LinkShipmentBody
) -> FulfillmentView:
    """Attach a Cargo shipment number a person found, after checking it with Cargo."""
    services.shipments.link(actor, fulfillment_id, body.number.strip())
    return services.work.show(actor, fulfillment_id)


@router.post("/fulfillments/{fulfillment_id}/shipment/create")
def create_shipment(services: ServicesDep, actor: Actor, fulfillment_id: int) -> FulfillmentView:
    """Ask Cargo for a shipment (the ``cargo`` shipment source only)."""
    services.shipments.create(actor, fulfillment_id)
    return services.work.show(actor, fulfillment_id)


@router.post("/fulfillments/{fulfillment_id}/shipment/not-created")
def shipment_not_created(
    services: ServicesDep, actor: Admin, fulfillment_id: int
) -> FulfillmentView:
    """A person checked Cargo and the uncertain shipment does not exist: it may be made again."""
    services.shipments.resolve_not_created(actor, fulfillment_id)
    return services.work.show(actor, fulfillment_id)


@router.post("/shipments/batch")
def create_shipments(services: ServicesDep, actor: Actor, body: BatchBody) -> dict[str, list[int]]:
    return services.shipments.create_batch(actor, body.fulfillment_ids)


# Inventory and jobs.


@router.post("/fulfillments/{fulfillment_id}/inventory/resolve")
def inventory_arrived(services: ServicesDep, actor: Actor, fulfillment_id: int) -> FulfillmentView:
    services.work.resolve_inventory(actor, fulfillment_id)
    return services.work.show(actor, fulfillment_id)


@router.post("/jobs/{job_id}/retry", status_code=204)
def retry_job(services: ServicesDep, actor: Admin, job_id: int) -> Response:
    """Try a failed Konimbo update again, after a person checked the order."""
    services.jobs.retry(actor, job_id)
    return Response(status_code=204)


# Duplicate review.


@router.get("/duplicates")
def duplicates(
    services: ServicesDep, actor: Actor, pending: bool = True
) -> list[DuplicateReviewView]:
    return services.duplicates.reviews(pending_only=pending)


@router.post("/duplicates/{review_id}/decision")
def decide(
    services: ServicesDep, actor: Actor, review_id: int, body: DecisionBody
) -> DuplicateReviewView:
    return services.duplicates.decide(
        actor, review_id, body.decision, duplicates=body.duplicates, note=body.note
    )


# Product nicknames.


@router.get("/aliases")
def aliases(services: ServicesDep, actor: Actor, item_key: str | None = None) -> list[AliasView]:
    return services.aliases.all(item_key)


@router.post("/aliases", status_code=201)
def add_alias(services: ServicesDep, actor: Actor, body: AliasBody) -> AliasView:
    return services.aliases.add(actor, line_id=body.line_id, alias=body.alias)


@router.delete("/aliases/{alias_id}", status_code=204)
def remove_alias(services: ServicesDep, actor: Actor, alias_id: int) -> Response:
    services.aliases.remove(actor, alias_id)
    return Response(status_code=204)


# Settings and sync.


@router.get("/settings")
def settings(services: ServicesDep, actor: Actor) -> SettingsView:
    return services.overview.settings()


@router.patch("/settings")
def update_settings(services: ServicesDep, actor: Admin, body: SettingsBody) -> SettingsView:
    return services.overview.update_settings(actor, body.changes)


@router.post("/settings/discover-statuses")
def discover_statuses(services: ServicesDep, actor: Admin) -> SettingsView:
    """Read recent Konimbo orders to learn the store's status names (one or a few requests)."""
    return services.overview.discover_statuses(actor)


@router.get("/printers")
def printers(services: ServicesDep, actor: Admin, credentials: Credentials) -> list[dict[str, Any]]:
    return services.overview.printers(actor, credentials)


@router.get("/sync")
def sync_status(services: ServicesDep, actor: Actor) -> SyncView:
    return services.overview.sync_view()


@router.post("/sync/run")
def run_sync(services: ServicesDep, actor: Admin) -> SyncRunView:
    return services.overview.run_sync()
