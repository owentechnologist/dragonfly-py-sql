"""End-to-end verification against a real Dragonfly instance (localhost:6379
by default -- override with DRAGONFLY_HOST/DRAGONFLY_PORT, or pass a full
connection URI via -U/--uri or DRAGONFLY_URI). Creates two throwaway indexes
(one HASH-backed, one JSON-backed), drives INSERT/SELECT/UPDATE through the
driver, and asserts against what's actually stored -- then drops both
indexes (and their docs) again. Not pytest: a standalone script so
`python3 test_driver.py` is enough to prove the driver works.
"""

import argparse
import os

from dragonfly_sql import Driver
from dragonfly_sql.client import make_client

HOST = os.environ.get("DRAGONFLY_HOST", "localhost")
PORT = int(os.environ.get("DRAGONFLY_PORT", "6379"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-U", "--uri",
        default=os.environ.get("DRAGONFLY_URI"),
        help="Connection URI, e.g. redis://user:pass@host:6379/0 "
             "(overrides DRAGONFLY_HOST/DRAGONFLY_PORT).",
    )
    return parser.parse_args()

HASH_INDEX = "idx_demo_products"
HASH_PREFIX = "demoproduct:"
JSON_INDEX = "idx_demo_orders"
JSON_PREFIX = "demoorder:"


def _drop_if_exists(client, index: str) -> None:
    try:
        client.execute_command("FT.DROPINDEX", index, "DD")
    except Exception:
        pass


def setup(client) -> None:
    _drop_if_exists(client, HASH_INDEX)
    _drop_if_exists(client, JSON_INDEX)
    client.execute_command(
        "FT.CREATE", HASH_INDEX, "ON", "HASH", "PREFIX", "1", HASH_PREFIX,
        "SCHEMA", "NAME", "TEXT", "SKU", "TAG", "PRICE", "NUMERIC", "IN_STOCK", "TAG",
    )
    client.execute_command(
        "FT.CREATE", JSON_INDEX, "ON", "JSON", "PREFIX", "1", JSON_PREFIX,
        "SCHEMA",
        "$.customer", "AS", "customer", "TAG",
        "$.total", "AS", "total", "NUMERIC",
        "$.status", "AS", "status", "TAG",
    )


def teardown(client) -> None:
    _drop_if_exists(client, HASH_INDEX)
    _drop_if_exists(client, JSON_INDEX)


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    assert condition, f"{label}: {detail}"


def run(driver: Driver, client) -> None:
    # -- HASH index: INSERT fills defaults + provided fields ----------------
    result = driver.execute(f"INSERT INTO {HASH_INDEX} (SKU, PRICE) VALUES ('SKU-1', 9.99)")
    key = result["inserted_key"]
    check("HASH INSERT returns a key under the index prefix", key.startswith(HASH_PREFIX), key)
    check("HASH INSERT fills unindexed-by-caller fields with defaults", result["fields"]["NAME"] == "", result["fields"])
    stored = client.hgetall(key)
    check("HASH INSERT actually wrote NAME/SKU/PRICE/IN_STOCK to the hash", stored.get("SKU") == "SKU-1" and stored.get("PRICE") == "9.99", stored)

    # -- HASH index: SELECT round-trips what INSERT wrote --------------------
    select_result = driver.execute(f"SELECT * FROM {HASH_INDEX} WHERE SKU = 'SKU-1'")
    check("SELECT finds the inserted row", len(select_result["rows"]) == 1, select_result)
    row = dict(zip(select_result["columns"], select_result["rows"][0]))
    check("SELECT returns the right PRICE", row["PRICE"] == 9.99, row)

    # -- HASH index: UPDATE resolves WHERE to one key and patches it --------
    update_result = driver.execute(f"UPDATE {HASH_INDEX} SET PRICE = 14.99 WHERE SKU = 'SKU-1'")
    check("UPDATE resolves to the same key INSERT created", update_result["updated_key"] == key, update_result)
    stored = client.hgetall(key)
    check("UPDATE actually changed PRICE in the hash", stored.get("PRICE") == "14.99", stored)

    # -- HASH index: UPDATE with an ambiguous WHERE refuses to guess ---------
    driver.execute(f"INSERT INTO {HASH_INDEX} (SKU, PRICE) VALUES ('SKU-2', 5.00)")
    driver.execute(f"INSERT INTO {HASH_INDEX} (SKU, PRICE) VALUES ('SKU-3', 5.00)")
    try:
        driver.execute(f"UPDATE {HASH_INDEX} SET PRICE = 1 WHERE PRICE = 5.00")
        check("UPDATE rejects a WHERE clause matching more than one row", False, "no exception raised")
    except ValueError as e:
        check("UPDATE rejects a WHERE clause matching more than one row", "more than one row" in str(e), str(e))

    # -- HASH index: GROUP BY / COUNT via FT.AGGREGATE -----------------------
    agg_result = driver.execute(f"SELECT IN_STOCK, COUNT(*) AS cnt FROM {HASH_INDEX} GROUP BY IN_STOCK")
    check("GROUP BY COUNT(*) returns one group (every row defaults IN_STOCK to '')", agg_result["rows"] == [["", 3]], agg_result)

    # -- HASH index: COUNT(DISTINCT col) via FT.AGGREGATE REDUCE COUNT_DISTINCT
    distinct_count = driver.execute(f"SELECT COUNT(DISTINCT SKU) AS cnt FROM {HASH_INDEX}")
    check("COUNT(DISTINCT SKU) counts the three distinct SKUs", distinct_count["rows"] == [[3]], distinct_count)

    try:
        driver.execute(f"SELECT SUM(DISTINCT PRICE) FROM {HASH_INDEX}")
        check("SUM(DISTINCT ...) is rejected", False, "no exception raised")
    except ValueError as e:
        check("SUM(DISTINCT ...) is rejected", "only supported inside COUNT" in str(e), str(e))

    # -- HASH index: SELECT DISTINCT dedupes via GROUP BY --------------------
    # Three rows exist here: SKU-1 at 14.99 (INSERT 9.99, then UPDATE) plus
    # SKU-2 and SKU-3 both at 5.0, so DISTINCT PRICE must collapse to two.
    distinct_result = driver.execute(f"SELECT DISTINCT PRICE FROM {HASH_INDEX}")
    check("SELECT DISTINCT PRICE collapses the two 5.0 rows into one", sorted(r[0] for r in distinct_result["rows"]) == [5.0, 14.99], distinct_result)

    try:
        driver.execute(f"SELECT DISTINCT * FROM {HASH_INDEX}")
        check("SELECT DISTINCT * is rejected", False, "no exception raised")
    except ValueError as e:
        check("SELECT DISTINCT * is rejected", "isn't supported" in str(e), str(e))

    try:
        driver.execute(f"SELECT DISTINCT COUNT(*) FROM {HASH_INDEX}")
        check("SELECT DISTINCT with an aggregate is rejected", False, "no exception raised")
    except ValueError as e:
        check("SELECT DISTINCT with an aggregate is rejected", "aggregate" in str(e), str(e))

    # -- JSON index: INSERT builds a nested doc from JSONPath fields ---------
    result = driver.execute(f"INSERT INTO {JSON_INDEX} (customer, total, status) VALUES ('acme', 42.5, 'open')")
    json_key = result["inserted_key"]
    check("JSON INSERT returns a key under the index prefix", json_key.startswith(JSON_PREFIX), json_key)
    doc = client.execute_command("JSON.GET", json_key, "$")
    check("JSON INSERT actually wrote a JSON document", '"customer":"acme"' in doc.replace(" ", ""), doc)

    # -- JSON index: SELECT decodes the "$" field back into real fields ------
    select_result = driver.execute(f"SELECT * FROM {JSON_INDEX} WHERE customer = 'acme'")
    row = dict(zip(select_result["columns"], select_result["rows"][0]))
    check("JSON SELECT returns decoded field values, not a raw JSON blob", row["status"] == "open", row)

    # -- JSON index: UPDATE writes through JSON.SET at the field's path ------
    driver.execute(f"UPDATE {JSON_INDEX} SET status = 'closed' WHERE customer = 'acme'")
    doc = client.execute_command("JSON.GET", json_key, "$.status")
    check("JSON UPDATE changed the field via JSON.SET", doc == '["closed"]', doc)

    # -- SHOW TABLES / DESCRIBE -----------------------------------------------
    tables = {r[0] for r in driver.execute("SHOW TABLES")["rows"]}
    check("SHOW TABLES lists both demo indexes", {HASH_INDEX, JSON_INDEX} <= tables, tables)
    describe = driver.execute(f"DESCRIBE {HASH_INDEX}")
    check("DESCRIBE lists the HASH index's real fields", describe["rows"] == [["IN_STOCK", "TAG"], ["NAME", "TEXT"], ["PRICE", "NUMERIC"], ["SKU", "TAG"]], describe)
    for alias in (
        f"DESC {HASH_INDEX}",
        f"DESCRIBE TABLE {HASH_INDEX}",
        f"DESC TABLE {HASH_INDEX}",
        f"\\d {HASH_INDEX}",
        f"EXEC sp_help {HASH_INDEX}",
        f"EXEC sp_help '{HASH_INDEX}'",
    ):
        aliased = driver.execute(alias)
        check(f"'{alias}' describes the same table as DESCRIBE", aliased == describe, aliased)


def main() -> None:
    args = parse_args()
    client = make_client(uri=args.uri, host=HOST, port=PORT)
    driver = Driver(client=client)
    setup(client)
    try:
        run(driver, client)
    finally:
        teardown(client)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
