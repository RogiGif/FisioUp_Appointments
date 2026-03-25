from django.db import migrations


def merge_legacy_reception_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")

    reception_group, _ = Group.objects.get_or_create(name="RECEPTION")
    legacy_group = Group.objects.filter(name="receptionist").first()
    if not legacy_group:
        return

    reception_group.user_set.add(*legacy_group.user_set.all())
    legacy_group.delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0052_clinicsettings_notify_password_reset"),
    ]

    operations = [
        migrations.RunPython(merge_legacy_reception_group, noop_reverse),
    ]

