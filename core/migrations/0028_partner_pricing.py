from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_rename_group_utente"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="price",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.CreateModel(
            name="Partner",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("active", models.BooleanField(default=True)),
                ("notes", models.TextField(blank=True, default="")),
            ],
        ),
        migrations.CreateModel(
            name="PartnerServicePrice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("price", models.DecimalField(decimal_places=2, max_digits=10)),
                ("partner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="service_prices", to="core.partner")),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="partner_prices", to="core.service")),
            ],
            options={
                "unique_together": {("partner", "service")},
            },
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="partner",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="clients", to="core.partner"),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="discount_type",
            field=models.CharField(choices=[("none", "Sem desconto"), ("percent", "Percentagem"), ("fixed", "Valor fixo")], default="none", max_length=20),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="discount_percent",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="discount_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="discount_label",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="appointment",
            name="base_price",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="appointment",
            name="partner",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="appointments", to="core.partner"),
        ),
        migrations.AddField(
            model_name="appointment",
            name="partner_price",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="appointment",
            name="discount_type",
            field=models.CharField(choices=[("none", "Sem desconto"), ("percent", "Percentagem"), ("fixed", "Valor fixo")], default="none", max_length=20),
        ),
        migrations.AddField(
            model_name="appointment",
            name="discount_value",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="appointment",
            name="final_price",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
    ]
