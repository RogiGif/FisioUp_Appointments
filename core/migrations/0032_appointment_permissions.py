from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_pricing_tiers"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="appointment",
            options={
                "ordering": ("date", "time"),
                "permissions": [
                    ("can_view_all_calendar", "Pode ver calendário global"),
                    ("can_book_for_any_professional", "Pode marcar para qualquer profissional"),
                    ("can_access_backoffice", "Pode aceder ao backoffice"),
                ],
            },
        ),
    ]
