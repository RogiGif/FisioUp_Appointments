from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_content_post"),
    ]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="is_independent",
            field=models.BooleanField(default=False, verbose_name="Subcontratado"),
        ),
        migrations.AddField(
            model_name="professional",
            name="hourly_rate",
            field=models.DecimalField(
                max_digits=10,
                decimal_places=2,
                null=True,
                blank=True,
                verbose_name="Valor por marcação",
            ),
        ),
    ]
