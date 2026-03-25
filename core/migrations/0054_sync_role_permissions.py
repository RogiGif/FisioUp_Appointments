from django.db import migrations


def sync_role_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    perms = list(
        Permission.objects.filter(
            codename__in=[
                "can_view_all_calendar",
                "can_book_for_any_professional",
                "can_access_backoffice",
            ]
        )
    )
    if not perms:
        return

    for group_name in ("ADMIN", "RECEPTION"):
        group, _ = Group.objects.get_or_create(name=group_name)
        group.permissions.add(*perms)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0053_merge_legacy_reception_group"),
    ]

    operations = [
        migrations.RunPython(sync_role_permissions, noop_reverse),
    ]

