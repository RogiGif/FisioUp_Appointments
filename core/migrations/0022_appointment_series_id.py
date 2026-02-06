from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_clinicsettings_email_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="series_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
    ]
