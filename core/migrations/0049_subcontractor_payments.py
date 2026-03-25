from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0048_migrate_and_remove_availability"),
    ]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="subcontract_percentage",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                verbose_name="Percentagem subcontrato",
            ),
        ),
        migrations.CreateModel(
            name="SubcontractorPaymentLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("appointment_date", models.DateField()),
                ("appointment_time", models.TimeField()),
                ("gross_amount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("percentage", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=5)),
                ("payable_amount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                (
                    "status",
                    models.CharField(
                        choices=[("unpaid", "Em aberto"), ("paid", "Pago"), ("void", "Anulado")],
                        default="unpaid",
                        max_length=20,
                    ),
                ),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("payment_reference", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "appointment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subcontract_payment",
                        to="core.appointment",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="subcontract_payments",
                        to="core.clientprofile",
                    ),
                ),
                (
                    "paid_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="subcontract_payments_paid",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subcontract_payments",
                        to="core.professional",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="subcontract_payments",
                        to="core.service",
                    ),
                ),
            ],
            options={
                "ordering": ("-appointment_date", "-appointment_time"),
            },
        ),
    ]
