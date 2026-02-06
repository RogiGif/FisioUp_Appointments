from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0020_email_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="clinic_email",
            field=models.EmailField(blank=True, max_length=254, verbose_name="Email da clínica"),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="reply_to_email",
            field=models.EmailField(blank=True, max_length=254, verbose_name="Email de resposta"),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="footer_text",
            field=models.TextField(blank=True, verbose_name="Texto de rodapé"),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="signature_text",
            field=models.TextField(blank=True, verbose_name="Assinatura"),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="logo",
            field=models.ImageField(blank=True, null=True, upload_to="clinic/", verbose_name="Logo"),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Atualizado em"),
        ),
    ]
