from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0072_service_slot_interval_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="pricing_tier_override",
            field=models.CharField(
                blank=True,
                choices=[
                    ("first", "Forçar 1ª consulta"),
                    ("followup", "Forçar seguintes"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
