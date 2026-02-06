from django.db import migrations


def rename_utente_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    try:
        utente = Group.objects.filter(name="Utente").first()
        if utente:
            utente.name = "Clientes"
            utente.save(update_fields=["name"])
        else:
            Group.objects.get_or_create(name="Clientes")
    except Exception:
        pass


def reverse_rename_utente_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    try:
        clientes = Group.objects.filter(name="Clientes").first()
        if clientes:
            clientes.name = "Utente"
            clientes.save(update_fields=["name"])
        else:
            Group.objects.get_or_create(name="Utente")
    except Exception:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_client_import_log"),
    ]

    operations = [
        migrations.RunPython(rename_utente_group, reverse_rename_utente_group),
    ]
