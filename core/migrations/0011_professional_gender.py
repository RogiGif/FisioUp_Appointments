from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_profile_photos"),
    ]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="gender",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
