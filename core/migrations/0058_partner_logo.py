from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0057_fill_contentpost_slugs"),
    ]

    operations = [
        migrations.AddField(
            model_name="partner",
            name="logo",
            field=models.ImageField(blank=True, null=True, upload_to="partners/", verbose_name="Logo"),
        ),
    ]
