from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


ROLE_GROUPS = ("ADMIN", "RECEPTION", "TECHNICIAN", "Cliente")


def _merge_alias_into(target_name, *aliases):
    target_group, _ = Group.objects.get_or_create(name=target_name)
    for alias in aliases:
        alias_group = Group.objects.filter(name=alias).first()
        if not alias_group or alias_group.id == target_group.id:
            continue
        target_group.user_set.add(*alias_group.user_set.all())
        target_group.permissions.add(*alias_group.permissions.all())
        alias_group.delete()
    return target_group


class Command(BaseCommand):
    help = "Garante grupos de role padrão e permissões base da app."

    def handle(self, *args, **options):
        # Canonical groups used by the app logic.
        groups = {name: Group.objects.get_or_create(name=name)[0] for name in ROLE_GROUPS}

        # Merge common legacy aliases so users/perms are preserved.
        groups["RECEPTION"] = _merge_alias_into(
            "RECEPTION",
            "receptionist",
            "Reception",
            "RECEPCAO",
            "Recepcao",
            "Receção",
        )
        groups["Cliente"] = _merge_alias_into(
            "Cliente",
            "Clientes",
            "CLIENTES",
            "cliente",
            "CLIENTE",
            "Utente",
        )
        groups["TECHNICIAN"] = _merge_alias_into(
            "TECHNICIAN",
            "Profissionais",
            "PROFISSIONAIS",
            "Tecnicos",
            "Técnicos",
            "Technician",
        )

        perm_codenames = [
            "can_view_all_calendar",
            "can_book_for_any_professional",
            "can_access_backoffice",
        ]
        perms = list(Permission.objects.filter(codename__in=perm_codenames))

        # Admin + reception get backoffice/calendar permissions.
        groups["ADMIN"].permissions.add(*perms)
        groups["RECEPTION"].permissions.add(*perms)

        self.stdout.write(
            self.style.SUCCESS(
                "Grupos sincronizados com sucesso: ADMIN, RECEPTION, TECHNICIAN e Cliente."
            )
        )
