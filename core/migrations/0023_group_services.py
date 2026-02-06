from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_appointment_series_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="service_type",
            field=models.CharField(choices=[("one_to_one", "Consulta"), ("group", "Turma")], default="one_to_one", max_length=20),
        ),
        migrations.AddField(
            model_name="service",
            name="capacity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="GroupSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("time", models.TimeField()),
                ("capacity", models.PositiveIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("scheduled", "Agendada"), ("completed", "Concluída"), ("cancelled", "Cancelada")], default="scheduled", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("professional", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="group_sessions", to="core.professional")),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_sessions", to="core.service")),
            ],
            options={
                "ordering": ("date", "time"),
                "unique_together": {("service", "professional", "date", "time")},
            },
        ),
        migrations.CreateModel(
            name="GroupEnrollment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("active", "Ativa"), ("cancelled", "Cancelada")], default="active", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_enrolments", to=settings.AUTH_USER_MODEL)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="enrolments", to="core.groupsession")),
            ],
            options={
                "ordering": ("-created_at",),
                "unique_together": {("session", "client")},
            },
        ),
    ]
