from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0058_partner_logo"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "Agendada"),
                    ("pending_confirmation", "Em confirmação"),
                    ("awaiting_validation", "A aguardar validação"),
                    ("no_show", "Falta"),
                    ("completed", "Concluída"),
                    ("in_debt", "Em dívida"),
                    ("cancelled", "Cancelada"),
                ],
                default="scheduled",
                max_length=20,
            ),
        ),
    ]
