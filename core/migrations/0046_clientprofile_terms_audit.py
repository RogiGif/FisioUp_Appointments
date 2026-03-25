from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0045_product_productcategory_stocklocation_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="accepted_terms_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="accepted_terms_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="accepted_terms_user_agent",
            field=models.TextField(blank=True, null=True),
        ),
    ]
