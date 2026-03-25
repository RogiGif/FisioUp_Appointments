from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_group_sessions_waitlist"),
    ]

    operations = [
        migrations.CreateModel(
            name="GroupSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("weekday", models.PositiveSmallIntegerField(choices=[
                    (0, "Segunda-feira"),
                    (1, "Terça-feira"),
                    (2, "Quarta-feira"),
                    (3, "Quinta-feira"),
                    (4, "Sexta-feira"),
                    (5, "Sábado"),
                    (6, "Domingo"),
                ])),
                ("time", models.TimeField()),
                ("start_date", models.DateField()),
                ("capacity", models.PositiveIntegerField(blank=True, null=True)),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("professional", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_schedules", to="core.professional")),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_schedules", to="core.service")),
            ],
            options={
                "ordering": ("weekday", "time"),
                "unique_together": {("professional", "weekday", "time")},
            },
        ),
        migrations.AddField(
            model_name="groupsession",
            name="schedule",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sessions", to="core.groupschedule"),
        ),
    ]
