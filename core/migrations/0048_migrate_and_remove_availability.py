from django.db import migrations


def migrate_availability(apps, schema_editor):
    Professional = apps.get_model("core", "Professional")
    Availability = apps.get_model("core", "Availability")
    WeeklySchedule = apps.get_model("core", "WeeklySchedule")
    WeeklyWorkingBlock = apps.get_model("core", "WeeklyWorkingBlock")

    for prof in Professional.objects.all():
        schedule = WeeklySchedule.objects.filter(professional=prof).first()
        if schedule and WeeklyWorkingBlock.objects.filter(weekly_schedule=schedule).exists():
            continue
        if not schedule:
            schedule = WeeklySchedule.objects.create(
                professional=prof,
                timezone="Europe/Lisbon",
                is_active=True,
            )
        for avail in Availability.objects.filter(professional=prof).order_by("weekday", "start_time"):
            WeeklyWorkingBlock.objects.create(
                weekly_schedule=schedule,
                weekday=avail.weekday,
                start_time=avail.start_time,
                end_time=avail.end_time,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0047_weekly_schedule"),
    ]

    operations = [
        migrations.RunPython(migrate_availability, migrations.RunPython.noop),
        migrations.DeleteModel(name="Availability"),
    ]
