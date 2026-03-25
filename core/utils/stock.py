from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from core.models import StockLocation, StockMovement, AppointmentConsumption

DEFAULT_LOCATION_NAME = "Armazém"


def get_default_location():
    location, _ = StockLocation.objects.get_or_create(name=DEFAULT_LOCATION_NAME)
    return location


def get_stock(product, location=None):
    qs = StockMovement.objects.filter(product=product, is_void=False)
    if location is not None:
        qs = qs.filter(location=location)
    total = qs.aggregate(total=Sum("quantity_base")).get("total") or Decimal("0.00")
    return total


def get_existing_consumption_totals(appointment):
    rows = (
        AppointmentConsumption.objects
        .filter(appointment=appointment)
        .values("product_id")
        .annotate(total=Sum("quantity_base"))
    )
    return {row["product_id"]: (row["total"] or Decimal("0.00")) for row in rows}


def reconcile_appointment_consumptions(appointment, items, *, user=None, location=None):
    """
    Reconciliates consumptions for an appointment.
    - items: list of (product, quantity_base) with positive quantities
    - existing consumption movements are voided and replaced
    """
    location = location or get_default_location()

    with transaction.atomic():
        StockMovement.objects.filter(
            appointment=appointment,
            movement_type=StockMovement.TYPE_CONSUMPTION,
            is_void=False,
        ).update(is_void=True)
        AppointmentConsumption.objects.filter(appointment=appointment).delete()

        for product, quantity_base in items:
            AppointmentConsumption.objects.create(
                appointment=appointment,
                product=product,
                quantity_base=quantity_base,
                created_by=user,
            )
            StockMovement.objects.create(
                product=product,
                location=location,
                movement_type=StockMovement.TYPE_CONSUMPTION,
                quantity_base=quantity_base * Decimal("-1"),
                appointment=appointment,
                created_by=user,
            )
