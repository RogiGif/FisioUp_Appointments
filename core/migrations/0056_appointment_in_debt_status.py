from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0055_merge_duplicate_role_groups"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "Agendada"),
                    ("pending_confirmation", "Em confirmação"),
                    ("completed", "Concluída"),
                    ("in_debt", "Em dívida"),
                    ("cancelled", "Cancelada"),
                ],
                default="scheduled",
                max_length=20,
            ),
        ),
    ]
