import json

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

from core.models import Appointment, AppointmentLog, AuditLog


class Command(BaseCommand):
    help = "Mostra o histórico de AppointmentLog e AuditLog de uma ou mais marcações."

    def add_arguments(self, parser):
        parser.add_argument("appointment_ids", nargs="+", type=int, help="IDs das marcações a inspecionar.")

    def handle(self, *args, **options):
        appointment_ids = options["appointment_ids"]
        appointments = (
            Appointment.objects.select_related(
                "client",
                "client__client_profile",
                "professional",
                "professional__user",
                "service",
            )
            .filter(id__in=appointment_ids)
            .order_by("id")
        )
        found_ids = set(appointments.values_list("id", flat=True))
        missing_ids = [appt_id for appt_id in appointment_ids if appt_id not in found_ids]
        if missing_ids:
            raise CommandError(f"Marcações não encontradas: {', '.join(str(v) for v in missing_ids)}")

        appointment_ct = ContentType.objects.get_for_model(Appointment, for_concrete_model=False)

        for appointment in appointments:
            client_name = "-"
            if appointment.client:
                profile = getattr(appointment.client, "client_profile", None)
                client_name = (
                    (profile.full_name if profile and profile.full_name else "")
                    or appointment.client.get_full_name()
                    or appointment.client.username
                )
            professional_name = "-"
            if appointment.professional and appointment.professional.user:
                professional_name = (
                    appointment.professional.user.get_full_name()
                    or appointment.professional.user.username
                )
            service_name = appointment.service.name if appointment.service else "-"

            self.stdout.write("")
            self.stdout.write(self.style.WARNING(f"Appointment #{appointment.id}"))
            self.stdout.write(
                str(
                    {
                        "date": str(appointment.date),
                        "time": str(appointment.time),
                        "client": client_name,
                        "professional": professional_name,
                        "service": service_name,
                        "status": appointment.status,
                        "is_paid": appointment.is_paid,
                        "paid_at": appointment.paid_at.isoformat() if appointment.paid_at else "",
                        "created_at": appointment.created_at.isoformat() if appointment.created_at else "",
                    }
                )
            )

            self.stdout.write("  AppointmentLog:")
            logs = AppointmentLog.objects.filter(appointment=appointment).select_related("actor").order_by("created_at", "id")
            if not logs.exists():
                self.stdout.write("    - sem logs")
            else:
                for log in logs:
                    actor = "-"
                    if log.actor:
                        actor = log.actor.get_full_name() or log.actor.get_username()
                    self.stdout.write(
                        "    "
                        + str(
                            {
                                "id": log.id,
                                "created_at": log.created_at.isoformat() if log.created_at else "",
                                "action": log.action,
                                "actor": actor,
                                "old_date": str(log.old_date) if log.old_date else "",
                                "old_time": str(log.old_time) if log.old_time else "",
                                "new_date": str(log.new_date) if log.new_date else "",
                                "new_time": str(log.new_time) if log.new_time else "",
                                "old_status": log.old_status or "",
                                "new_status": log.new_status or "",
                                "note": log.note or "",
                            }
                        )
                    )

            self.stdout.write("  AuditLog:")
            audit_logs = (
                AuditLog.objects
                .filter(content_type=appointment_ct, object_id=appointment.id)
                .select_related("actor")
                .order_by("created_at", "id")
            )
            if not audit_logs.exists():
                self.stdout.write("    - sem logs")
            else:
                for log in audit_logs:
                    self.stdout.write(
                        "    "
                        + json.dumps(
                            {
                                "id": log.id,
                                "created_at": log.created_at.isoformat() if log.created_at else "",
                                "category": log.category,
                                "action": log.action,
                                "source": log.source,
                                "actor": log.actor_display or "",
                                "message": log.message or "",
                                "request": f"{log.request_method or '-'} {log.request_path or '-'}",
                                "before": log.before or {},
                                "after": log.after or {},
                                "metadata": log.metadata or {},
                            },
                            ensure_ascii=False,
                        )
                    )
