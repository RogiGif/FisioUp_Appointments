from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_clientprofile_postal_designation"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlockedSlot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("time", models.TimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_blocked_slots",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blocked_slots",
                        to="core.professional",
                    ),
                ),
            ],
            options={
                "ordering": ["-date", "-time"],
                "unique_together": {("professional", "date", "time")},
            },
        ),
    ]
