"""
Project package init.

Use PyMySQL as a drop-in replacement for MySQLdb so environments that
don't have ``mysqlclient`` compiled (common on cPanel shared hosting)
can still use Django's mysql backend.
"""

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except Exception:
    # If PyMySQL isn't available, Django will raise a clear DB backend error.
    pass
