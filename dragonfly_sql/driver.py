r"""SQL driver over Dragonfly's built-in search (FT.*) indexes.

Each FT index is a table:
- SHOW TABLES, DESCRIBE <table> / SHOW COLUMNS FROM <table> introspect indexes,
  as do the dialect aliases DESC, DESCRIBE/DESC TABLE, \d, and EXEC sp_help.
- SELECT ... FROM <table> [WHERE ...] [GROUP BY ...] [ORDER BY ...] [LIMIT ...]
  translates to FT.SEARCH, or FT.AGGREGATE when GROUP BY/an aggregate function
  is present. WHERE translation is type-aware (TAG/NUMERIC/TEXT, from FT.INFO).
- SELECT DISTINCT <col>, ... becomes a GROUP BY on those columns, since an
  FT.AGGREGATE GROUPBY with no REDUCE already emits one row per unique
  combination.
- INSERT INTO <table> (col, ...) VALUES (...) reads the index's schema via
  FT.INFO, generates a new key under the index's declared prefix, fills every
  indexed field with a type-appropriate default, overlays the given columns,
  and writes it with HSET (HASH index) or JSON.SET (JSON index).
- UPDATE <table> SET col = val, ... WHERE ... resolves WHERE to exactly one
  matching row via FT.SEARCH, then updates that row's fields the same
  HASH/JSON-aware way.

JOIN, DELETE, DROP, CREATE, and ALTER are not supported.
"""

import json
import re
import uuid

import sqlglot
from sqlglot import exp

from . import translate
from .client import make_client
from .schema import IndexSchema, coerce_value, default_value, fetch_schema

_SHOW_TABLES_RE = re.compile(r"^\s*SHOW\s+TABLES\s*;?\s*$", re.IGNORECASE)
_DESCRIBE_RE = re.compile(
    r"^\s*(?:DESCRIBE(?:\s+TABLE)?|DESC(?:\s+TABLE)?|SHOW\s+COLUMNS\s+FROM|\\d|EXEC\s+sp_help)\s+"
    r"'?([A-Za-z0-9_:]+)'?\s*;?\s*$",
    re.IGNORECASE,
)

# Index names may contain ':' (e.g. "idx:battle_v21"). sqlglot treats a bare
# ':' as a named-parameter placeholder, not an identifier character, so a
# colon-containing table name after FROM/INTO/UPDATE is masked with a plain
# placeholder before parsing and restored afterwards.
_TABLE_NAME_RE = re.compile(
    r"(\bFROM\s+|\bINTO\s+|\bUPDATE\s+)([A-Za-z0-9_]+(?::[A-Za-z0-9_]+)+)", re.IGNORECASE
)

_WRITE_NODES = {exp.Delete: "DELETE", exp.Drop: "DROP", exp.Create: "CREATE", exp.Alter: "ALTER"}


def _mask_colon_table(sql: str) -> tuple[str, dict]:
    mapping = {}

    def repl(m: re.Match) -> str:
        placeholder = f"__tbl{len(mapping)}__"
        mapping[placeholder] = m.group(2)
        return m.group(1) + placeholder

    return _TABLE_NAME_RE.sub(repl, sql), mapping


def _rows_to_dicts(rows: list) -> list[dict]:
    return [dict(zip(row[0::2], row[1::2])) for row in rows if isinstance(row, list)]


def _coerce_agg_value(value, reducer: str, field: str | None, field_types: dict):
    # COUNT reports a row count, not the schema type of any field.
    if reducer == "COUNT":
        return int(float(value)) if value is not None else None
    return coerce_value(value, field_types.get(field))


def _decode_json_doc_field(fields: dict) -> dict:
    # A JSON index with no RETURN clause replies with a single "$" field
    # holding the whole document as a JSON-encoded string.
    if fields.keys() == {"$"}:
        return json.loads(fields["$"])
    return fields


def _set_json_path(doc: dict, path: str, value) -> None:
    """Assign `value` into `doc` at a simple dotted JSONPath ("$.a.b").
    Bracket/array-index paths aren't supported -- FT.CREATE ... ON JSON
    schemas built by hand almost always use plain dotted paths."""
    if not path.startswith("$"):
        raise ValueError(f"Unsupported JSONPath '{path}' -- only simple '$.a.b' paths are supported.")
    parts = path[1:].lstrip(".").split(".") if path != "$" else []
    if not parts:
        raise ValueError(f"Field with JSONPath '{path}' targets the whole document -- not supported for INSERT.")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class Driver:
    def __init__(self, client=None, **client_kwargs):
        self.client = client if client is not None else make_client(**client_kwargs)

    # -- dispatch -----------------------------------------------------------

    def execute(self, sql: str) -> dict:
        sql = (sql or "").strip()
        if not sql:
            raise ValueError("sql must not be empty")

        if _SHOW_TABLES_RE.match(sql):
            return self._show_tables()
        described = _DESCRIBE_RE.match(sql)
        if described:
            return self._describe_table(described.group(1))

        masked_sql, table_mapping = _mask_colon_table(sql)
        try:
            parsed = sqlglot.parse_one(masked_sql)
        except Exception as e:
            raise ValueError(f"Could not parse SQL: {e}")

        if isinstance(parsed, exp.Select):
            return self._select(parsed, table_mapping)
        if isinstance(parsed, exp.Insert):
            return self._insert(parsed, table_mapping)
        if isinstance(parsed, exp.Update):
            return self._update(parsed, table_mapping)
        for node_cls, label in _WRITE_NODES.items():
            if isinstance(parsed, node_cls):
                raise ValueError(f"{label} is not supported by this driver.")
        raise ValueError("Only SELECT, INSERT, UPDATE, SHOW TABLES, and DESCRIBE/SHOW COLUMNS FROM are supported.")

    # -- introspection --------------------------------------------------------

    def _show_tables(self) -> dict:
        names = sorted(self.client.execute_command("FT._LIST"))
        rows = []
        for name in names:
            s = fetch_schema(self.client, name)
            rows.append([s.name, s.key_type, s.num_docs])
        return {"columns": ["table_name", "key_type", "num_docs"], "rows": rows}

    def _describe_table(self, table: str) -> dict:
        s = fetch_schema(self.client, table)
        rows = [[f.name, f.type] for f in s.fields]
        return {"columns": ["field_name", "type"], "rows": rows}

    # -- SELECT ---------------------------------------------------------------

    def _ft_search(
        self,
        table: str,
        s: IndexSchema,
        query: str,
        limit: int = 10,
        offset: int = 0,
        return_fields: list[str] | None = None,
        sort_by: str | None = None,
        sort_desc: bool = False,
        no_content: bool = False,
    ) -> dict:
        if not no_content and return_fields is None:
            vector_fields = {f.name for f in s.fields if f.type == "VECTOR"}
            if vector_fields:
                return_fields = [f.name for f in s.fields if f.name not in vector_fields]

        if return_fields is not None and len(return_fields) == 0:
            no_content = True
            return_fields = None

        cmd = ["FT.SEARCH", table, query]
        if return_fields is not None:
            cmd += ["RETURN", str(len(return_fields)), *return_fields]
        if sort_by:
            cmd += ["SORTBY", sort_by, "DESC" if sort_desc else "ASC"]
        if no_content:
            cmd.append("NOCONTENT")
        cmd += ["LIMIT", str(offset), str(limit), "DIALECT", "2"]

        reply = self.client.execute_command(*cmd)
        total = reply[0] if reply and isinstance(reply[0], int) else 0
        docs = []
        rest = reply[1:]
        i = 0
        while i < len(rest):
            doc_id = rest[i]
            if no_content:
                docs.append({"id": doc_id})
                i += 1
            else:
                raw_fields = rest[i + 1] if i + 1 < len(rest) else []
                raw_fields = raw_fields if isinstance(raw_fields, dict) else dict(zip(raw_fields[0::2], raw_fields[1::2]))
                docs.append({"id": doc_id, "fields": _decode_json_doc_field(raw_fields)})
                i += 2
        return {"total": total, "results": docs}

    def _ft_aggregate(self, table: str, query: str, pipeline: str) -> dict:
        cmd = ["FT.AGGREGATE", table, query, *pipeline.split(), "DIALECT", "2"]
        reply = self.client.execute_command(*cmd)
        return {"results": _rows_to_dicts(reply)}

    def _select(self, parsed: exp.Select, table_mapping: dict) -> dict:
        if parsed.args.get("joins"):
            raise ValueError("JOIN isn't supported by this driver.")

        from_exp = parsed.args.get("from_") or parsed.args.get("from")
        if from_exp is None or not isinstance(from_exp.this, exp.Table):
            raise ValueError("SELECT must have a single FROM <table>.")
        table = table_mapping.get(from_exp.this.name, from_exp.this.name)

        s = fetch_schema(self.client, table)
        field_types = s.field_types

        where_exp = parsed.args.get("where")
        query_str = translate.where_query_string(where_exp, s)

        group_exp = parsed.args.get("group")
        group_fields = [translate.field_name(c, field_types) for c in group_exp.expressions] if group_exp else []

        plain_fields, aggregates = translate.select_targets(parsed.expressions, field_types)

        if parsed.args.get("distinct"):
            if group_fields:
                raise ValueError("SELECT DISTINCT can't be combined with GROUP BY -- use one or the other.")
            if aggregates:
                raise ValueError("SELECT DISTINCT can't be combined with an aggregate function -- GROUP BY the columns you want instead.")
            if "*" in plain_fields:
                raise ValueError("SELECT DISTINCT * isn't supported -- list the columns to dedupe on explicitly.")
            # A GROUPBY with no REDUCE emits one row per unique combination of
            # the grouped fields, which is exactly what DISTINCT asks for.
            group_fields = plain_fields

        order_exp = parsed.args.get("order")
        order_fields = []
        if order_exp:
            for o in order_exp.expressions:
                if not isinstance(o.this, exp.Column):
                    raise ValueError(f"ORDER BY must reference a plain column, not '{o.this.sql()}'")
                order_fields.append((o.this.name, bool(o.args.get("desc"))))

        limit_exp, offset_exp = parsed.args.get("limit"), parsed.args.get("offset")
        limit = int(translate.literal_value(limit_exp.expression)) if limit_exp else 10
        offset = int(translate.literal_value(offset_exp.expression)) if offset_exp else 0

        is_aggregate = bool(aggregates) or bool(group_fields)
        if is_aggregate:
            for f in plain_fields:
                if f != "*" and f not in group_fields:
                    raise ValueError(f"'{f}' is selected but not in GROUP BY -- add it to GROUP BY or wrap it in an aggregate.")
            alias_set = {a for _, _, a in aggregates}
            for name, _ in order_fields:
                if name not in group_fields and name not in alias_set:
                    raise ValueError(f"ORDER BY '{name}' must be a GROUP BY column or an aggregate alias.")

            pipeline_parts = [
                "GROUPBY " + str(len(group_fields)) + " " + " ".join(f"@{f}" for f in group_fields)
                if group_fields
                else "GROUPBY 0"
            ]
            for reducer, field, alias in aggregates:
                if reducer == "COUNT":
                    pipeline_parts.append(f"REDUCE COUNT 0 AS {alias}")
                else:
                    pipeline_parts.append(f"REDUCE {reducer} 1 @{field} AS {alias}")
            if order_fields:
                sort_tokens = [tok for name, desc in order_fields for tok in (f"@{name}", "DESC" if desc else "ASC")]
                pipeline_parts.append("SORTBY " + str(len(sort_tokens)) + " " + " ".join(sort_tokens))
            pipeline_parts.append(f"LIMIT {offset} {limit}")
            pipeline = " ".join(pipeline_parts)

            result = self._ft_aggregate(table, query_str, pipeline)
            columns = group_fields + [a for _, _, a in aggregates]
            agg_by_alias = {alias: (reducer, field) for reducer, field, alias in aggregates}
            rows = [
                [
                    _coerce_agg_value(row.get(c), *agg_by_alias[c], field_types)
                    if c in agg_by_alias
                    else coerce_value(row.get(c), field_types.get(c))
                    for c in columns
                ]
                for row in result["results"]
            ]
            return {
                "table": table,
                "dragonfly_command": f'FT.AGGREGATE {table} "{query_str}" {pipeline}',
                "columns": columns,
                "rows": rows,
            }

        sort_by, sort_desc = None, False
        if len(order_fields) > 1:
            raise ValueError("A plain SELECT only supports ordering by one field -- use GROUP BY for multi-field sorting.")
        if order_fields:
            name, desc = order_fields[0]
            sort_by = translate.resolve_name(name, field_types)
            if sort_by is None:
                raise ValueError(f"'{name}' is not a field on this table. Available fields: {sorted(field_types)}")
            sort_desc = desc

        select_all = not plain_fields or "*" in plain_fields
        return_fields = None if select_all else plain_fields

        result = self._ft_search(
            table, s, query_str, limit=limit, offset=offset, return_fields=return_fields, sort_by=sort_by, sort_desc=sort_desc
        )
        docs = result["results"]
        if select_all:
            field_columns = sorted({k for d in docs for k in d.get("fields", {})})
            columns = ["redis_key"] + field_columns
            rows = [
                [d.get("id")] + [coerce_value(d.get("fields", {}).get(c), field_types.get(c)) for c in field_columns]
                for d in docs
            ]
        else:
            columns = plain_fields
            rows = [[coerce_value(d.get("fields", {}).get(c), field_types.get(c)) for c in columns] for d in docs]

        sort_clause = f" SORTBY {sort_by} {'DESC' if sort_desc else 'ASC'}" if sort_by else ""
        return {
            "table": table,
            "total": result["total"],
            "dragonfly_command": f'FT.SEARCH {table} "{query_str}"{sort_clause} LIMIT {offset} {limit}',
            "columns": columns,
            "rows": rows,
        }

    # -- INSERT -----------------------------------------------------------------

    def _write_row(self, s: IndexSchema, key: str, row: dict) -> None:
        if s.key_type == "JSON":
            doc: dict = {}
            for f in s.fields:
                if f.name in row:
                    _set_json_path(doc, f.path, row[f.name])
            self.client.execute_command("JSON.SET", key, "$", json.dumps(doc))
        else:
            if not row:
                raise ValueError("INSERT produced no fields to store -- the index has no defaultable fields.")
            self.client.hset(key, mapping=row)

    def _insert(self, parsed: exp.Insert, table_mapping: dict) -> dict:
        schema_node = parsed.this
        if not isinstance(schema_node, exp.Schema) or not isinstance(schema_node.this, exp.Table):
            raise ValueError("INSERT must name columns: INSERT INTO <table> (col, ...) VALUES (...)")
        table = table_mapping.get(schema_node.this.name, schema_node.this.name)
        columns = [ident.this for ident in schema_node.expressions]

        values_exp = parsed.args.get("expression")
        if not isinstance(values_exp, exp.Values) or not values_exp.expressions:
            raise ValueError("INSERT requires a VALUES clause.")

        s = fetch_schema(self.client, table)

        inserted = []
        for tuple_exp in values_exp.expressions:
            values = [translate.literal_value(v) for v in tuple_exp.expressions]
            if len(values) != len(columns):
                raise ValueError(f"INSERT column count ({len(columns)}) doesn't match VALUES count ({len(values)}).")

            provided = {}
            for col, val in zip(columns, values):
                field = s.field(col)
                provided[field.name] = val

            row = {f.name: v for f in s.fields if (v := default_value(f.type)) is not None}
            row.update(provided)

            key = f"{s.prefix}{uuid.uuid4().hex}"
            self._write_row(s, key, row)
            inserted.append({"key": key, "fields": row})

        if len(inserted) == 1:
            return {"table": table, "inserted_key": inserted[0]["key"], "fields": inserted[0]["fields"]}
        return {"table": table, "inserted_keys": [r["key"] for r in inserted], "rows": inserted}

    # -- UPDATE -----------------------------------------------------------------

    def _apply_update(self, s: IndexSchema, key: str, updates: dict) -> None:
        if s.key_type == "JSON":
            for name, value in updates.items():
                f = s.field(name)
                self.client.execute_command("JSON.SET", key, f.path, json.dumps(value))
        else:
            self.client.hset(key, mapping=updates)

    def _update(self, parsed: exp.Update, table_mapping: dict) -> dict:
        if not isinstance(parsed.this, exp.Table):
            raise ValueError("UPDATE must target a single table.")
        table = table_mapping.get(parsed.this.name, parsed.this.name)

        s = fetch_schema(self.client, table)
        field_types = s.field_types

        set_exprs = parsed.args.get("expressions") or []
        if not set_exprs:
            raise ValueError("UPDATE requires a SET clause.")
        updates = {}
        for e in set_exprs:
            if not isinstance(e, exp.EQ):
                raise ValueError(f"Unsupported SET expression: '{e.sql()}'")
            field = translate.field_name(e.this, field_types)
            updates[field] = translate.literal_value(e.expression)

        where_exp = parsed.args.get("where")
        if where_exp is None:
            raise ValueError("UPDATE requires a WHERE clause identifying the target row -- this driver has no full-table UPDATE.")
        query_str = translate.translate_where(where_exp.this, field_types)

        matches = self._ft_search(table, s, query_str, limit=2, offset=0, no_content=True)
        ids = [d["id"] for d in matches["results"]]
        if not ids:
            raise ValueError(f"No row on '{table}' matches WHERE {query_str!r} -- nothing to update.")
        if len(ids) > 1:
            raise ValueError(
                f"WHERE {query_str!r} matches more than one row on '{table}' -- UPDATE requires a WHERE "
                "clause that identifies exactly one row (e.g. its unique id field)."
            )
        key = ids[0]

        self._apply_update(s, key, updates)
        return {"table": table, "updated_key": key, "fields": updates}
