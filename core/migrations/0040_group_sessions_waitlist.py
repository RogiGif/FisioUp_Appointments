from django.db import migrations, models
from django.utils import timezone


def migrate_group_enrollment_statuses(apps, schema_editor):
    GroupEnrollment = apps.get_model("core", "GroupEnrollment")
    GroupEnrollment.objects.filter(status="active").update(status="booked")


def rollback_group_enrollment_statuses(apps, schema_editor):
    GroupEnrollment = apps.get_model("core", "GroupEnrollment")
    GroupEnrollment.objects.filter(status="booked").update(status="active")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_appointment_symptomatology_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="allow_waitlist",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="group_cancel_hours",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="groupsession",
            name="duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="groupsession",
            name="notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="groupenrollment",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=timezone.now),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="groupenrollment",
            name="status",
            field=models.CharField(
                choices=[
                    ("booked", "Confirmada"),
                    ("waitlist", "Lista de espera"),
                    ("cancelled", "Cancelada"),
                    ("attended", "Presença"),
                    ("no_show", "Falta"),
                ],
                default="booked",
                max_length=20,
            ),
        ),
        migrations.RunPython(migrate_group_enrollment_statuses, rollback_group_enrollment_statuses),
    ]
