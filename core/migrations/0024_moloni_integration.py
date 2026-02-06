from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_group_services"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="moloni_customer_id",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.CreateModel(
            name="MoloniIntegration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("access_token", models.TextField(blank=True)),
                ("refresh_token", models.TextField(blank=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Integração Moloni",
                "verbose_name_plural": "Integração Moloni",
            },
        ),
    ]
