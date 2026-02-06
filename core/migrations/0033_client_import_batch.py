from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_appointment_permissions"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientImportBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("original_filename", models.CharField(blank=True, max_length=255)),
                ("validate_nif", models.BooleanField(default=True)),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="client_import_batches", to="auth.user")),
            ],
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Lote de importação (clientes)",
                "verbose_name_plural": "Lotes de importação (clientes)",
            },
        ),
        migrations.CreateModel(
            name="ClientImportRow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("row_key", models.PositiveIntegerField()),
                ("full_name", models.CharField(blank=True, max_length=255)),
                ("nif", models.CharField(blank=True, max_length=20)),
                ("phone", models.CharField(blank=True, max_length=30)),
                ("email", models.CharField(blank=True, max_length=255)),
                ("address_line1", models.CharField(blank=True, max_length=255)),
                ("postal_code", models.CharField(blank=True, max_length=20)),
                ("city", models.CharField(blank=True, max_length=120)),
                ("county", models.CharField(blank=True, max_length=120)),
                ("district", models.CharField(blank=True, max_length=120)),
                ("valid_vat", models.BooleanField(default=False)),
                ("missing_email", models.BooleanField(default=True)),
                ("duplicate_in_file", models.BooleanField(default=False)),
                ("exists_in_db", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rows", to="core.clientimportbatch")),
            ],
            options={
                "ordering": ["row_key"],
                "verbose_name": "Linha de importação (clientes)",
                "verbose_name_plural": "Linhas de importação (clientes)",
                "unique_together": {("batch", "row_key")},
            },
        ),
    ]
