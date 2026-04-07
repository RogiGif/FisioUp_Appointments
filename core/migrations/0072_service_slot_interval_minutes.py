from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0071_moloniintegration_customer_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="slot_interval_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
