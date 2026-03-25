from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0046_clientprofile_terms_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="WeeklySchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("timezone", models.CharField(default="Europe/Lisbon", max_length=50)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "professional",
                    models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_schedule", to="core.professional"),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WeeklyWorkingBlock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("weekday", models.IntegerField(choices=[(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday")])),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("location", models.CharField(blank=True, max_length=120)),
                (
                    "weekly_schedule",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="blocks", to="core.weeklyschedule"),
                ),
            ],
            options={"ordering": ["weekly_schedule", "weekday", "start_time"]},
        ),
        migrations.CreateModel(
            name="WeeklyBreakBlock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("weekday", models.IntegerField(choices=[(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday")])),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                (
                    "weekly_schedule",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="breaks", to="core.weeklyschedule"),
                ),
            ],
            options={"ordering": ["weekly_schedule", "weekday", "start_time"]},
        ),
    ]
