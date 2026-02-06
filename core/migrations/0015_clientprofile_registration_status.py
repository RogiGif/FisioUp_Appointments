from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_clientprofile_terms_rgpd"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="registration_status",
            field=models.CharField(choices=[("approved", "Aprovado"), ("pending", "Pendente"), ("rejected", "Rejeitado")], default="approved", max_length=20),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="registration_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="registration_reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="registration_reviewed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name="reviewed_clients", to="auth.user"),
        ),
    ]
