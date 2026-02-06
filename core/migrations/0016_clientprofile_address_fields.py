from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_clientprofile_registration_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="district",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="county",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="locality",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
