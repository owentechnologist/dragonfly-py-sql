"""A Python SQL driver over Dragonfly's built-in search indexes (FT.* commands).

    from dragonfly_sql import Driver

    driver = Driver(host="localhost", port=6379)
    driver.execute("SELECT * FROM idx_products WHERE BRAND = 'acme' LIMIT 5")
    driver.execute("INSERT INTO idx_products (BRAND, PRICE) VALUES ('acme', 19.99)")
    driver.execute("UPDATE idx_products SET PRICE = 24.99 WHERE SKU = 'ACME-1'")

See driver.py for the full supported-SQL notes.
"""

from .driver import Driver

__all__ = ["Driver"]
