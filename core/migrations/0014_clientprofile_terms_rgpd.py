from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_clientprofile_gender"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="terms_accepted",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="rgpd_accepted",
            field=models.BooleanField(default=False),
        ),
    ]
