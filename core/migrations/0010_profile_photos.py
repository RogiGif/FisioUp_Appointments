from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_alter_availability_professional"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="profile_photo",
            field=models.ImageField(blank=True, null=True, upload_to="profiles/clients/"),
        ),
        migrations.AddField(
            model_name="professional",
            name="profile_photo",
            field=models.ImageField(blank=True, null=True, upload_to="profiles/professionals/"),
        ),
    ]
