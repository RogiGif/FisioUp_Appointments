from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0041_group_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="groupschedule",
            name="name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="groupsession",
            name="name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
    ]
