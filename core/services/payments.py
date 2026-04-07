from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.models import Appointment, CashMovement, CashSession, ClientPayment, ClientPaymentAllocation


MONEY_STEP = Decimal("0.01")


def quantize_money(value):
    return Decimal(value or "0.00").quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def _apply_discount(amount, discount_type, discount_value):
    amount = quantize_money(amount)
    discount_value = quantize_money(discount_value)
    discount_applied = Decimal("0.00")
    if discount_type == "percent":
        discount_applied = amount * (discount_value / Decimal("100"))
    elif discount_type == "fixed":
        discount_applied = min(discount_value, amount)
    discount_applied = quantize_money(discount_applied)
    final_amount = amount - discount_applied
    if final_amount < 0:
        final_amount = Decimal("0.00")
    return quantize_money(final_amount), discount_applied


def build_appointment_settlement(appointment, pricing_mode=None, manual_final_price=None):
    pricing_mode = pricing_mode or appointment.settlement_pricing_mode or Appointment.SETTLEMENT_PRICING_MODE_AUTO
    discount_type = appointment.discount_type or "none"
    discount_value = quantize_money(appointment.discount_value or Decimal("0.00"))

    if pricing_mode == Appointment.SETTLEMENT_PRICING_MODE_AUTO:
        return {
            "pricing_mode": pricing_mode,
            "partner": appointment.partner,
            "discount_type": discount_type,
            "discount_value": discount_value,
            "final_price": quantize_money(appointment.final_price or Decimal("0.00")),
        }

    if pricing_mode == Appointment.SETTLEMENT_PRICING_MODE_WITHOUT_PARTNER:
        final_price, _ = _apply_discount(
            appointment.base_price_applied or appointment.base_price or Decimal("0.00"),
            discount_type,
            discount_value,
        )
        return {
            "pricing_mode": pricing_mode,
            "partner": None,
            "discount_type": discount_type,
            "discount_value": discount_value,
            "final_price": final_price,
        }

    manual_price = quantize_money(manual_final_price)
    return {
        "pricing_mode": Appointment.SETTLEMENT_PRICING_MODE_MANUAL,
        "partner": appointment.partner,
        "discount_type": "none",
        "discount_value": Decimal("0.00"),
        "final_price": manual_price if manual_price > 0 else Decimal("0.00"),
    }


def sync_appointment_payment_flags(appointment, *, paid_at_hint=None):
    paid_total = appointment.get_paid_amount()
    outstanding = appointment.get_outstanding_amount()
    changed_fields = []

    should_be_paid = outstanding <= 0
    paid_at_value = paid_at_hint if should_be_paid else None
    if should_be_paid and not paid_at_value:
        paid_at_value = appointment.paid_at or timezone.now()

    if appointment.is_paid != should_be_paid:
        appointment.is_paid = should_be_paid
        changed_fields.append("is_paid")

    if appointment.paid_at != paid_at_value:
        appointment.paid_at = paid_at_value
        changed_fields.append("paid_at")

    return changed_fields, paid_total, outstanding


def _payment_local_date(payment):
    return timezone.localtime(payment.received_at).date() if payment.received_at else timezone.localdate()


def build_client_payment_cash_description(payment):
    client_name = payment.client_profile.full_name if payment.client_profile else "Cliente"
    allocations = list(
        payment.allocations.select_related("appointment__service", "group_monthly_charge__service")[:2]
    )
    if len(allocations) == 1:
        allocation = allocations[0]
        if allocation.appointment_id and allocation.appointment and allocation.appointment.service:
            return f"Pagamento cliente · {client_name} · {allocation.appointment.service.name}"
        if allocation.group_monthly_charge_id:
            class_name = (
                allocation.group_monthly_charge.class_name
                or getattr(allocation.group_monthly_charge.service, "name", "")
                or "Turma"
            )
            return f"Pagamento cliente · {client_name} · {class_name}"
    if allocations:
        return f"Pagamento cliente · {client_name} · {payment.allocations.count()} liquidações"
    return f"Pagamento cliente · {client_name}"


def get_client_payment_moloni_state(payment):
    if payment.moloni_document_id:
        return ClientPayment.MOLONI_SYNC_SYNCED, ""
    if payment.status == ClientPayment.STATUS_VOID:
        return ClientPayment.MOLONI_SYNC_SKIPPED, "Pagamento anulado."
    if (payment.amount_received or Decimal("0.00")) <= 0:
        return ClientPayment.MOLONI_SYNC_SKIPPED, "Pagamento sem valor faturável."
    if payment.allocations.count() == 0:
        return ClientPayment.MOLONI_SYNC_SKIPPED, "Pagamento sem marcações ou mensalidades afetadas."
    if payment.unallocated_amount > 0:
        return ClientPayment.MOLONI_SYNC_PENDING, "Pagamento com valor por afetar antes da faturação."
    if not payment.client_profile_id:
        return ClientPayment.MOLONI_SYNC_SKIPPED, "Pagamento sem ficha de cliente associada."
    if not (payment.client_profile.nif or "").strip():
        return ClientPayment.MOLONI_SYNC_SKIPPED, "Cliente sem NIF para emissão automática."
    return ClientPayment.MOLONI_SYNC_PENDING, ""


def sync_client_payment_moloni_state(payment, *, save=True):
    new_status, status_note = get_client_payment_moloni_state(payment)
    changed_fields = []
    if payment.moloni_sync_status != new_status:
        payment.moloni_sync_status = new_status
        changed_fields.append("moloni_sync_status")
    if payment.moloni_sync_error != status_note:
        payment.moloni_sync_error = status_note
        changed_fields.append("moloni_sync_error")
    if save and changed_fields:
        payment.save(update_fields=changed_fields + ["updated_at"])
    return new_status, status_note


def ensure_client_payment_cash_movement(payment, *, session=None, notes_append=""):
    if payment.cash_movement_id:
        return payment.cash_movement, False, ""
    if payment.status != ClientPayment.STATUS_POSTED:
        return None, False, "O pagamento está anulado e não pode entrar em caixa."

    payment_date = _payment_local_date(payment)
    target_session = session
    if target_session is None:
        target_session = (
            CashSession.objects
            .filter(status=CashSession.STATUS_OPEN, session_date=payment_date)
            .order_by("-opened_at", "-id")
            .first()
        )
    if target_session is None:
        return None, False, f"Sem sessão de caixa aberta para {payment_date:%d/%m/%Y}."
    if target_session.status != CashSession.STATUS_OPEN:
        return None, False, "A sessão de caixa selecionada está fechada."
    if target_session.session_date != payment_date:
        return None, False, "A sessão de caixa não corresponde à data do pagamento."

    movement_notes = payment.notes or ""
    if notes_append:
        movement_notes = f"{movement_notes}\n{notes_append}".strip() if movement_notes else notes_append.strip()

    movement = CashMovement.objects.create(
        session=target_session,
        movement_type=CashMovement.TYPE_IN,
        source_type=CashMovement.SOURCE_CLIENT_PAYMENT,
        payment_method=payment.payment_method,
        amount=payment.amount_received,
        description=build_client_payment_cash_description(payment),
        notes=movement_notes,
        client_profile=payment.client_profile,
        created_by=payment.created_by,
        happened_at=payment.received_at or timezone.now(),
    )
    payment.cash_movement = movement
    payment.save(update_fields=["cash_movement", "updated_at"])
    return movement, True, ""


def sync_client_payment_integrations(payment, *, cash_session=None, cash_notes_append=""):
    movement, movement_created, cash_message = ensure_client_payment_cash_movement(
        payment,
        session=cash_session,
        notes_append=cash_notes_append,
    )
    moloni_status, moloni_note = sync_client_payment_moloni_state(payment, save=True)
    return {
        "cash_movement": movement,
        "cash_movement_created": movement_created,
        "cash_message": cash_message,
        "moloni_status": moloni_status,
        "moloni_note": moloni_note,
    }


@transaction.atomic
def create_client_payment(
    *,
    client_profile,
    amount_received,
    payment_method,
    created_by=None,
    received_at=None,
    reference="",
    notes="",
    appointment_targets=None,
    cash_session=None,
):
    payment = ClientPayment.objects.create(
        client_profile=client_profile,
        amount_received=quantize_money(amount_received),
        payment_method=payment_method,
        created_by=created_by,
        received_at=received_at or timezone.now(),
        reference=reference or "",
        notes=notes or "",
    )

    remaining = payment.amount_received
    created_allocations = []
    for appointment in appointment_targets or []:
        if remaining <= 0:
            break
        outstanding = appointment.get_outstanding_amount()
        if outstanding <= 0:
            continue
        allocated_amount = min(outstanding, remaining)
        allocation = ClientPaymentAllocation.objects.create(
            payment=payment,
            appointment=appointment,
            allocated_amount=allocated_amount,
        )
        created_allocations.append(allocation)
        remaining = quantize_money(remaining - allocated_amount)

    integration_state = sync_client_payment_integrations(payment, cash_session=cash_session)
    return payment, created_allocations, integration_state
