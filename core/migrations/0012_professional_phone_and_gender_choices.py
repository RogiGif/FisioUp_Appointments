from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_professional_gender"),
    ]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="phone",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AlterField(
            model_name="professional",
            name="gender",
            field=models.CharField(blank=True, choices=[("masculino", "Masculino"), ("feminino", "Feminino")], max_length=20),
        ),
    ]
