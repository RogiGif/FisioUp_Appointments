from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0037_professional_independent_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="is_paid",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="appointment",
            name="paid_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
