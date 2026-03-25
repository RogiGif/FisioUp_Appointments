from django.db import migrations


def merge_group_into(Group, source_name, target_name):
    source = Group.objects.filter(name=source_name).first()
    if not source:
        return
    target, _ = Group.objects.get_or_create(name=target_name)
    target.user_set.add(*source.user_set.all())
    target.permissions.add(*source.permissions.all())
    source.delete()


def dedupe_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")

    # Canonical groups used by current app logic.
    canonical_groups = ("ADMIN", "RECEPTION", "TECHNICIAN", "Cliente")
    for group_name in canonical_groups:
        Group.objects.get_or_create(name=group_name)

    # Reception legacy alias.
    for alias in ("receptionist", "Reception", "RECEPCAO", "Recepcao", "Receção"):
        merge_group_into(Group, alias, "RECEPTION")

    # Client legacy aliases.
    for alias in ("Clientes", "CLIENTES", "cliente", "CLIENTE"):
        merge_group_into(Group, alias, "Cliente")

    # Technician/professional legacy aliases.
    for alias in ("Profissionais", "PROFISSIONAIS", "Tecnicos", "Técnicos", "Technician"):
        merge_group_into(Group, alias, "TECHNICIAN")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0054_sync_role_permissions"),
    ]

    operations = [
        migrations.RunPython(dedupe_groups, noop_reverse),
    ]

