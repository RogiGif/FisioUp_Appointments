from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_moloni_integration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clientprofile",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="client_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
