from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_partner_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="partner",
            name="discount_type",
            field=models.CharField(choices=[("none", "Sem desconto"), ("percent", "Percentagem"), ("fixed", "Valor fixo")], default="none", max_length=20),
        ),
        migrations.AddField(
            model_name="partner",
            name="discount_percent",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="partner",
            name="discount_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="partner",
            name="discount_label",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
