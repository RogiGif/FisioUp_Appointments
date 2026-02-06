from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_client_import_batch"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="require_complete_profile",
            field=models.BooleanField(default=False),
        ),
    ]
