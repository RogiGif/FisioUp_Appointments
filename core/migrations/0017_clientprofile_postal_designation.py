from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_clientprofile_address_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="postal_designation",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
