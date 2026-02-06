from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission


class Command(BaseCommand):
    help = "Cria grupos padrão e atribui permissões de backoffice/calendário."

    def handle(self, *args, **options):
        admin_group, _ = Group.objects.get_or_create(name="ADMIN")
        reception_group, _ = Group.objects.get_or_create(name="RECEPTION")
        technician_group, _ = Group.objects.get_or_create(name="TECHNICIAN")

        perm_codenames = [
            "can_view_all_calendar",
            "can_book_for_any_professional",
            "can_access_backoffice",
        ]
        perms = list(Permission.objects.filter(codename__in=perm_codenames))

        admin_group.permissions.add(*perms)
        reception_group.permissions.add(*perms)

        self.stdout.write(self.style.SUCCESS("Grupos e permissões atualizados com sucesso."))
