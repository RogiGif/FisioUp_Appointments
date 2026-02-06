from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_clientprofile_user_nullable"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientImportLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("file_name", models.CharField(blank=True, max_length=255)),
                ("created_count", models.IntegerField(default=0)),
                ("updated_count", models.IntegerField(default=0)),
                ("skipped_count", models.IntegerField(default=0)),
                ("error_count", models.IntegerField(default=0)),
                ("summary", models.TextField(blank=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="client_import_logs", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Importação de clientes",
                "verbose_name_plural": "Importações de clientes",
            },
        ),
    ]
