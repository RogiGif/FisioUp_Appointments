from django.db import migrations


def add_partner_permissions_to_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    try:
        admin_group = Group.objects.get(name="ADMIN")
    except Group.DoesNotExist:
        return

    perm_codenames = [
        "view_partner",
        "add_partner",
        "change_partner",
        "view_partnerserviceprice",
        "add_partnerserviceprice",
        "change_partnerserviceprice",
    ]

    perms = Permission.objects.filter(codename__in=perm_codenames)
    admin_group.permissions.add(*perms)


def remove_partner_permissions_from_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    try:
        admin_group = Group.objects.get(name="ADMIN")
    except Group.DoesNotExist:
        return

    perm_codenames = [
        "view_partner",
        "add_partner",
        "change_partner",
        "view_partnerserviceprice",
        "add_partnerserviceprice",
        "change_partnerserviceprice",
    ]

    perms = Permission.objects.filter(codename__in=perm_codenames)
    admin_group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_partner_pricing"),
    ]

    operations = [
        migrations.RunPython(
            add_partner_permissions_to_admin_group,
            remove_partner_permissions_from_admin_group,
        ),
    ]
