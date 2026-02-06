from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_clinicsettings"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("pending_registration", "Pedido de registo"),
                            ("new_booking", "Nova marcação"),
                            ("reschedule_client", "Reagendamento pelo utente"),
                            ("reschedule_clinic", "Reagendamento pela clínica"),
                            ("cancel_client", "Cancelamento pelo utente"),
                            ("cancel_clinic", "Cancelamento pela clínica"),
                            ("generic", "Genérico"),
                        ],
                        default="generic",
                        max_length=64,
                    ),
                ),
                ("to", models.TextField()),
                ("subject", models.CharField(max_length=255)),
                ("body_text", models.TextField(blank=True)),
                ("body_html", models.TextField(blank=True)),
                ("status", models.CharField(default="sent", max_length=20)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
