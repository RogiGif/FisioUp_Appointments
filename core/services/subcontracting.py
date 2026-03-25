from decimal import Decimal, ROUND_HALF_UP

from core.models import Appointment, SubcontractorPaymentLine


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _get_client_profile(appointment):
    if not appointment or not getattr(appointment, "client_id", None):
        return None
    try:
        return appointment.client.client_profile
    except Exception:
        return None


def sync_subcontractor_payout(appointment, *, actor=None):
    """
    Cria/atualiza o payout de subcontratado quando a marcação é concluída.
    Se a marcação deixar de estar concluída, marca o payout como anulado.
    """
    if not appointment or not getattr(appointment, "professional_id", None):
        return None

    professional = appointment.professional
    if (
        appointment.status != Appointment.STATUS_COMPLETED
        or not professional
        or not professional.is_independent
    ):
        line = SubcontractorPaymentLine.objects.filter(appointment=appointment).first()
        if line and line.status != SubcontractorPaymentLine.STATUS_VOID:
            line.status = SubcontractorPaymentLine.STATUS_VOID
            line.save(update_fields=["status", "updated_at"])
        return None

    percentage = professional.subcontract_percentage or Decimal("0.00")
    gross_amount = appointment.final_price or Decimal("0.00")
    payable_amount = _quantize_money(gross_amount * (percentage / Decimal("100")))
    client_profile = _get_client_profile(appointment)

    defaults = {
        "professional": professional,
        "client": client_profile,
        "service": appointment.service,
        "appointment_date": appointment.date,
        "appointment_time": appointment.time,
        "gross_amount": gross_amount,
        "percentage": percentage,
        "payable_amount": payable_amount,
        "status": SubcontractorPaymentLine.STATUS_UNPAID,
    }
    line, created = SubcontractorPaymentLine.objects.get_or_create(
        appointment=appointment,
        defaults=defaults,
    )
    if created:
        return line

    updates = {}
    if line.status == SubcontractorPaymentLine.STATUS_VOID:
        updates["status"] = SubcontractorPaymentLine.STATUS_UNPAID
    if line.status != SubcontractorPaymentLine.STATUS_PAID:
        if line.professional_id != professional.id:
            updates["professional"] = professional
        if line.client_id != (client_profile.id if client_profile else None):
            updates["client"] = client_profile
        if line.service_id != (appointment.service_id or None):
            updates["service"] = appointment.service
        if line.appointment_date != appointment.date:
            updates["appointment_date"] = appointment.date
        if line.appointment_time != appointment.time:
            updates["appointment_time"] = appointment.time
        if line.gross_amount != gross_amount:
            updates["gross_amount"] = gross_amount
        if line.percentage != percentage:
            updates["percentage"] = percentage
        if line.payable_amount != payable_amount:
            updates["payable_amount"] = payable_amount
    if updates:
        for key, value in updates.items():
            setattr(line, key, value)
        line.save(update_fields=list(updates.keys()) + ["updated_at"])
    return line
