import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

try:
    import pymysql
except ImportError:
    pass
else:
    pymysql.install_as_MySQLdb()

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
