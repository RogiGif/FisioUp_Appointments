from django.core.management.base import BaseCommand

from core.models import MoloniIntegration
from core.services.audit import log_audit_event
from core.services.moloni_sync import push_local_customers, run_bidirectional_reconciliation, sync_customers


class Command(BaseCommand):
    help = "Sincroniza clientes entre Moloni e app"

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true", help="Sync completa")
        parser.add_argument("--since", type=str, default=None, help="Data para sync incremental (YYYY-MM-DD)")
        parser.add_argument("--push-local", action="store_true", help="Envia clientes locais com NIF para a Moloni")
        parser.add_argument("--bidirectional", action="store_true", help="Executa sync bidirecional")

    def handle(self, *args, **options):
        if options["bidirectional"]:
            result = run_bidirectional_reconciliation(full=options["full"], since=options["since"])
            log_audit_event(
                category="integrations",
                action="moloni_reconciliation_cron",
                actor=None,
                instance=MoloniIntegration.get_solo(),
                source="cron:moloni_sync_customers",
                message="Reconciliação Moloni executada por comando.",
                after=result,
                metadata={"mode": "bidirectional", "full": bool(options["full"]), "since": options["since"] or ""},
            )
            incoming = result["incoming"]
            outgoing = result["outgoing"]
            self.stdout.write(
                "Moloni -> app: "
                f"processados={incoming['processed']} criados={incoming['created']} "
                f"atualizados={incoming['updated']} ignorados={incoming['skipped']} erros={incoming['errors']}"
            )
            self.stdout.write(
                "app -> Moloni: "
                f"processados={outgoing['processed']} enviados={outgoing['pushed']} "
                f"ignorados={outgoing['skipped']} erros={outgoing['errors']}"
            )
            return

        if options["push_local"]:
            result = push_local_customers(full=options["full"], since=options["since"])
            log_audit_event(
                category="integrations",
                action="moloni_push_local_cron",
                actor=None,
                instance=MoloniIntegration.get_solo(),
                source="cron:moloni_sync_customers",
                message="Envio de clientes locais para Moloni executado por comando.",
                after=result,
                metadata={"mode": "push_local", "full": bool(options["full"]), "since": options["since"] or ""},
            )
            self.stdout.write(
                f"Processados: {result['processed']} | Enviados: {result['pushed']} | "
                f"Ignorados: {result['skipped']} | Erros: {result['errors']}"
            )
            return

        result = sync_customers(full=options["full"], since=options["since"])
        log_audit_event(
            category="integrations",
            action="moloni_sync_cron",
            actor=None,
            instance=MoloniIntegration.get_solo(),
            source="cron:moloni_sync_customers",
            message="Sincronização Moloni executada por comando.",
            after=result,
            metadata={"mode": "pull_remote", "full": bool(options["full"]), "since": options["since"] or ""},
        )
        self.stdout.write(
            f"Modo: {result['mode']} | Processados: {result['processed']} | "
            f"Criados: {result['created']} | Atualizados: {result['updated']} | "
            f"Ignorados: {result['skipped']} | Erros: {result['errors']}"
        )
