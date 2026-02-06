from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_blockedslot"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClinicSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("clinic_name", models.CharField(default="FisioUp", max_length=120)),
                (
                    "notification_emails",
                    models.TextField(
                        blank=True,
                        help_text="Emails separados por vírgulas ou por linha. Ex: rececao@..., gerente@...",
                    ),
                ),
                ("from_email", models.EmailField(blank=True, max_length=254)),
                ("notify_admin_on_pending_registration", models.BooleanField(default=True)),
                ("notify_clinic_on_new_booking", models.BooleanField(default=True)),
                ("notify_clinic_on_client_reschedule", models.BooleanField(default=True)),
                ("notify_clinic_on_client_cancel", models.BooleanField(default=True)),
                ("notify_client_on_clinic_changes", models.BooleanField(default=True)),
                ("notify_professional_on_new_booking", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Configuração da clínica",
                "verbose_name_plural": "Configuração da clínica",
            },
        ),
    ]
