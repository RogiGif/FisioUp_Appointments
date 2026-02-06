from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0035_alter_partner_options_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ContentPost",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("slug", models.SlugField(blank=True, unique=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("news", "Notícia"),
                            ("promo", "Promoção"),
                            ("notice", "Aviso"),
                            ("partner", "Parceria"),
                            ("project", "Projeto"),
                        ],
                        default="news",
                        max_length=20,
                    ),
                ),
                ("excerpt", models.TextField(blank=True)),
                ("body", models.TextField()),
                ("cover_image", models.ImageField(blank=True, null=True, upload_to="posts/")),
                ("is_featured", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[("draft", "Rascunho"), ("published", "Publicado")],
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="posts_created",
                        to="auth.user",
                    ),
                ),
            ],
            options={
                "ordering": ["-is_featured", "-published_at", "-created_at"],
            },
        ),
    ]
