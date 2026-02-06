from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_professional_phone_and_gender_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="gender",
            field=models.CharField(blank=True, choices=[("masculino", "Masculino"), ("feminino", "Feminino")], max_length=20),
        ),
    ]
