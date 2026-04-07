from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import Appointment


class Command(BaseCommand):
    help = "Audita possíveis duplicados de marcações na agenda por data/hora/profissional/cliente/serviço."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default="", help="Filtrar por data exata (YYYY-MM-DD).")
        parser.add_argument("--start-date", type=str, default="", help="Data inicial (YYYY-MM-DD).")
        parser.add_argument("--end-date", type=str, default="", help="Data final (YYYY-MM-DD).")
        parser.add_argument("--time", type=str, default="", help="Filtrar por hora exata (HH:MM).")
        parser.add_argument("--limit", type=int, default=50, help="Número máximo de grupos a listar.")

    def handle(self, *args, **options):
        date_value = (options.get("date") or "").strip()
        start_date = (options.get("start_date") or "").strip()
        end_date = (options.get("end_date") or "").strip()
        time_value = (options.get("time") or "").strip()
        limit = max(1, int(options.get("limit") or 50))

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

        self.stdout.write(f"Grupos duplicados encontrados: {len(duplicate_groups)}")
        if not duplicate_groups:
            return

        grouped_rows = defaultdict(list)
        for appointment in appointments.order_by("date", "time", "professional_id", "client_id", "id"):
            key = (
                appointment.date,
                appointment.time,
                appointment.professional_id,
                appointment.client_id,
                appointment.service_id,
            )
            grouped_rows[key].append(appointment)

        for index, group in enumerate(duplicate_groups, start=1):
            key = (
                group["date"],
                group["time"],
                group["professional_id"],
                group["client_id"],
                group["service_id"],
            )
            rows = grouped_rows.get(key, [])
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"[{index}] {group['date']} {group['time']} | "
                    f"prof={group['professional_id']} client={group['client_id']} "
                    f"service={group['service_id']} | total={group['total']}"
                )
            )
            for appointment in rows:
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
                self.stdout.write(
                    "  "
                    + str(
                        {
                            "id": appointment.id,
                            "professional": professional_name,
                            "client": client_name,
                            "service": service_name,
                            "status": appointment.status,
                            "is_paid": appointment.is_paid,
                            "paid_at": appointment.paid_at.isoformat() if appointment.paid_at else "",
                            "created_at": appointment.created_at.isoformat() if appointment.created_at else "",
                        }
                    )
                )
