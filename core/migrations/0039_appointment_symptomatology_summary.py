from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_appointment_paid_fields"),
    ]

    operations = [
        migrations.RenameField(
            model_name="appointment",
            old_name="notes",
            new_name="symptomatology",
        ),
        migrations.AddField(
            model_name="appointment",
            name="summary",
            field=models.TextField(blank=True, default=""),
        ),
    ]
