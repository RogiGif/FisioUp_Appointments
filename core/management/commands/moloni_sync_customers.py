from django.core.management.base import BaseCommand

from core.services.moloni_sync import sync_customers


class Command(BaseCommand):
    help = "Sincroniza clientes do Moloni"

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true", help="Sync completa")
        parser.add_argument("--since", type=str, default=None, help="Data para sync incremental (YYYY-MM-DD)")

    def handle(self, *args, **options):
        result = sync_customers(full=options["full"], since=options["since"])
        self.stdout.write(
            f"Criados: {result['created']} | Atualizados: {result['updated']} | "
            f"Ignorados: {result['skipped']} | Erros: {result['errors']}"
        )
