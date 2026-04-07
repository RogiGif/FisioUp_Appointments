from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from core.models import Appointment
from core.services.audit import log_audit_event, snapshot_instance


class Command(BaseCommand):
    help = (
        "Resolve duplicados históricos de marcações de forma conservadora. "
        "Por defeito corre em dry-run e só remove canceladas redundantes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default="", help="Filtrar por data exata (YYYY-MM-DD).")
        parser.add_argument("--start-date", type=str, default="", help="Data inicial (YYYY-MM-DD).")
        parser.add_argument("--end-date", type=str, default="", help="Data final (YYYY-MM-DD).")
        parser.add_argument("--time", type=str, default="", help="Filtrar por hora exata (HH:MM).")
        parser.add_argument("--limit", type=int, default=50, help="Número máximo de grupos a processar.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica a limpeza. Sem esta flag o comando só mostra o plano.",
        )

    def handle(self, *args, **options):
        date_value = (options.get("date") or "").strip()
        start_date = (options.get("start_date") or "").strip()
        end_date = (options.get("end_date") or "").strip()
        time_value = (options.get("time") or "").strip()
        limit = max(1, int(options.get("limit") or 50))
        apply_changes = bool(options.get("apply"))

        appointments = Appointment.objects.select_related(
            "client",
            "client__client_profile",
            "professional",
            "professional__user",
            "service",
        )
        if date_value:
            appointments = appointments.filter(date=date_value)
        else:
            if start_date:
                appointments = appointments.filter(date__gte=start_date)
            if end_date:
                appointments = appointments.filter(date__lte=end_date)
        if time_value:
            appointments = appointments.filter(time=time_value)

        duplicate_groups = list(
            appointments.values("date", "time", "professional_id", "client_id", "service_id")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
            .order_by("-total", "date", "time")[:limit]
        )

        self.stdout.write(
            f"Grupos duplicados encontrados: {len(duplicate_groups)} | modo={'APPLY' if apply_changes else 'DRY-RUN'}"
        )
        if not duplicate_groups:
            return

        grouped_rows = defaultdict(list)
        for appointment in appointments.order_by("date", "time", "professional_id", "client_id", "created_at", "id"):
            key = (
                appointment.date,
                appointment.time,
                appointment.professional_id,
                appointment.client_id,
                appointment.service_id,
            )
            grouped_rows[key].append(appointment)

        deleted_total = 0
        skipped_total = 0
        manual_total = 0

        for index, group in enumerate(duplicate_groups, start=1):
            key = (
                group["date"],
                group["time"],
                group["professional_id"],
                group["client_id"],
                group["service_id"],
            )
            rows = grouped_rows.get(key, [])
            cancelled = [appt for appt in rows if appt.status == Appointment.STATUS_CANCELLED]
            non_cancelled = [appt for appt in rows if appt.status != Appointment.STATUS_CANCELLED]

            keep = None
            delete_candidates = []
            reason = ""

            if len(non_cancelled) == 1 and cancelled:
                keep = non_cancelled[0]
                delete_candidates = cancelled
                reason = "manter a marcação não cancelada e remover canceladas redundantes"
            elif not non_cancelled and len(cancelled) > 1:
                keep = sorted(cancelled, key=lambda appt: (appt.created_at or 0, appt.id), reverse=True)[0]
                delete_candidates = [appt for appt in cancelled if appt.id != keep.id]
                reason = "todas canceladas; manter a mais recente e remover canceladas redundantes"
            else:
                manual_total += 1
                skipped_total += len(rows)
                self.stdout.write("")
                self.stdout.write(
                    self.style.WARNING(
                        f"[{index}] {group['date']} {group['time']} | "
                        f"prof={group['professional_id']} client={group['client_id']} "
                        f"service={group['service_id']} | total={group['total']} | revisão manual"
                    )
                )
                for appt in rows:
                    self.stdout.write(
                        "  "
                        + str(
                            {
                                "id": appt.id,
                                "status": appt.status,
                                "created_at": appt.created_at.isoformat() if appt.created_at else "",
                            }
                        )
                    )
                continue

            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{index}] {group['date']} {group['time']} | "
                    f"prof={group['professional_id']} client={group['client_id']} "
                    f"service={group['service_id']} | manter #{keep.id} | "
                    f"remover {[appt.id for appt in delete_candidates]} | {reason}"
                )
            )

            if not apply_changes or not delete_candidates:
                continue

            with transaction.atomic():
                for appt in delete_candidates:
                    log_audit_event(
                        category="appointments",
                        action="duplicate_cleanup_deleted",
                        actor=None,
                        instance=appt,
                        source="management_command",
                        message="Marcação redundante removida por limpeza de duplicados.",
                        before=snapshot_instance(
                            appt,
                            fields=[
                                "client_id",
                                "professional_id",
                                "service_id",
                                "date",
                                "time",
                                "status",
                                "is_paid",
                                "paid_at",
                                "completed_at",
                                "completed_by_id",
                                "final_price",
                            ],
                        ),
                        after={},
                        metadata={
                            "kept_appointment_id": keep.id,
                            "duplicate_group": {
                                "date": str(group["date"]),
                                "time": str(group["time"]),
                                "professional_id": group["professional_id"],
                                "client_id": group["client_id"],
                                "service_id": group["service_id"],
                            },
                        },
                    )
                    appt.delete()
                    deleted_total += 1

        self.stdout.write("")
        self.stdout.write(
            f"Resumo | removidas={deleted_total} | revisão_manual={manual_total} | ignoradas={skipped_total}"
        )
