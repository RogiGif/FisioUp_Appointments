import importlib
import os
import sys
import traceback


def section(title):
    print(f"\n== {title} ==")


def show(name, value):
    print(f"{name}: {value}")


def import_status(module_name):
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        print(f"{module_name}: FAIL {type(exc).__name__}: {exc}")
        return None

    version = getattr(module, "__version__", "")
    print(f"{module_name}: OK {version}".rstrip())
    return module


section("Runtime")
show("executable", sys.executable)
show("version", sys.version.replace("\n", " "))
show("cwd", os.getcwd())
show("file", __file__)

section("Python path")
for item in sys.path[:8]:
    print(item)

section("Packages")
pymysql = import_status("pymysql")
if pymysql is not None:
    pymysql.install_as_MySQLdb()
    print("pymysql.install_as_MySQLdb: OK")
import_status("MySQLdb")
import_status("django")
import_status("whitenoise")
import_status("PIL")
import_status("openpyxl")

section("Django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
try:
    import django

    django.setup()
    print("django.setup: OK")

    from django.conf import settings

    show("ENV", getattr(settings, "ENV", None))
    show("DEBUG", settings.DEBUG)
    show("ALLOWED_HOSTS", settings.ALLOWED_HOSTS)
    db = settings.DATABASES["default"]
    show("DB_ENGINE", db.get("ENGINE"))
    show("DB_NAME", db.get("NAME"))
    show("DB_USER", db.get("USER"))
    show("DB_HOST", db.get("HOST"))
    show("DB_PORT", db.get("PORT"))

    from django.core.management import call_command

    call_command("check")
    print("manage.py check: OK")

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    show("DB_SELECT_1", row)
except Exception:
    traceback.print_exc()
    sys.exit(1)

section("Passenger WSGI")
try:
    passenger_wsgi = importlib.import_module("passenger_wsgi")
    show("module_file", getattr(passenger_wsgi, "__file__", None))
    show("application", getattr(passenger_wsgi, "application", None))
except Exception:
    traceback.print_exc()
    sys.exit(2)

section("Request smoke tests")
try:
    from django.test import Client

    for host in ("marcacoes.fisio-up.pt", "www.marcacoes.fisio-up.pt"):
        cases = (
            ("plain", {}),
            ("secure", {"secure": True}),
            ("forwarded_proto", {"HTTP_X_FORWARDED_PROTO": "https"}),
        )
        for label, kwargs in cases:
            client = Client(HTTP_HOST=host)
            response = client.get("/health/", **kwargs)
            print(
                f"GET /health/ {label} host={host}: "
                f"status={response.status_code} "
                f"location={response.get('location')} "
                f"content_type={response.get('content-type')} "
                f"body={response.content[:200]!r}"
            )
except Exception:
    traceback.print_exc()
    sys.exit(3)

print("\nDIAG_OK")
