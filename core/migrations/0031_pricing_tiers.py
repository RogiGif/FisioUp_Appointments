from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0030_partner_discount_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="pricing_mode",
            field=models.CharField(choices=[("single", "Preço único"), ("first_followup", "1ª consulta / seguintes")], default="single", max_length=20),
        ),
        migrations.AddField(
            model_name="service",
            name="price_first",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="service",
            name="price_followup",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="partnerserviceprice",
            name="pricing_mode",
            field=models.CharField(choices=[("single", "Preço único"), ("first_followup", "1ª consulta / seguintes")], default="single", max_length=20),
        ),
        migrations.AddField(
            model_name="partnerserviceprice",
            name="price_first",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="partnerserviceprice",
            name="price_followup",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="session_index",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="appointment",
            name="pricing_tier",
            field=models.CharField(choices=[("single", "single"), ("first", "first"), ("followup", "followup")], default="single", max_length=20),
        ),
        migrations.AddField(
            model_name="appointment",
            name="base_price_applied",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="appointment",
            name="partner_price_applied",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="appointment",
            name="discount_applied",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
    ]
