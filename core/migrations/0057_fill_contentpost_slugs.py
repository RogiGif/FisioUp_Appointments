from django.db import migrations
from django.db.models import Q
from django.utils.text import slugify


def fill_missing_contentpost_slugs(apps, schema_editor):
    ContentPost = apps.get_model("core", "ContentPost")

    queryset = ContentPost.objects.filter(Q(slug__isnull=True) | Q(slug=""))
    for post in queryset.iterator():
        base_slug = slugify(post.title or "") or "post"
        candidate = base_slug
        counter = 2

        while ContentPost.objects.exclude(pk=post.pk).filter(slug=candidate).exists():
            candidate = f"{base_slug}-{counter}"
            counter += 1

        post.slug = candidate
        post.save(update_fields=["slug"])


def noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0056_appointment_in_debt_status"),
    ]

    operations = [
        migrations.RunPython(fill_missing_contentpost_slugs, noop_reverse),
    ]
