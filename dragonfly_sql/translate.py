"""SQL WHERE/SELECT-list translation into RediSearch query syntax.

Ported from dragonfly-harness's read-only SQL tool (mcp/src/dfly_mcp/tools/sql.py),
which is the proven source of truth for how each SQL operator maps to a
type-correct `@field:...` clause. Kept independent of that package so this
driver has no import-time dependency on the MCP server.
"""

import re

from sqlglot import exp

from .schema import IndexSchema

_TAG_ESCAPE_RE = re.compile(r'([,.<>{}\[\]"\':;!@#$%^&*()\-+=~\s])')


def escape_tag_value(value: object) -> str:
    return _TAG_ESCAPE_RE.sub(r"\\\1", str(value))


def build_filter_clause(field: str, value: object, field_type: str | None) -> str:
    """One `@field:...` clause, using the syntax the field's real type needs
    (TAG -> {escaped}, NUMERIC -> [lo hi], TEXT -> plain/quoted)."""
    if field_type == "NUMERIC":
        if isinstance(value, (list, tuple)) and len(value) == 2:
            lo, hi = value
        else:
            lo = hi = value
        return f"@{field}:[{lo} {hi}]"
    if field_type == "TAG":
        values = value if isinstance(value, (list, tuple)) else [value]
        escaped = "|".join(escape_tag_value(v) for v in values)
        return f"@{field}:{{{escaped}}}"
    value = str(value)
    return f'@{field}:"{value}"' if " " in value else f"@{field}:{value}"


def resolve_name(name: str, candidates: dict) -> str | None:
    if name in candidates:
        return name
    for real in candidates:
        if real.lower() == name.lower():
            return real
    return None


def field_name(node: exp.Expression, field_types: dict) -> str:
    if not isinstance(node, exp.Column):
        raise ValueError(f"Expected a plain column reference, got: '{node.sql()}'")
    resolved = resolve_name(node.name, field_types)
    if resolved is None:
        raise ValueError(f"'{node.name}' is not a field on this table. Available fields: {sorted(field_types)}")
    return resolved


def literal_value(node: exp.Expression):
    if isinstance(node, exp.Neg):
        return -literal_value(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        text = node.this
        return int(text) if re.fullmatch(r"-?\d+", text) else float(text)
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, exp.Null):
        return None
    raise ValueError(f"Unsupported literal value: '{node.sql()}'")


def _numeric_bound_clause(field: str, op: str, value) -> str:
    if op == ">":
        return f"@{field}:[({value} +inf]"
    if op == ">=":
        return f"@{field}:[{value} +inf]"
    if op == "<":
        return f"@{field}:[-inf ({value}]"
    return f"@{field}:[-inf {value}]"  # "<="


def _in_clause(field: str, field_type: str, values: list) -> str:
    if field_type == "TAG":
        return build_filter_clause(field, values, "TAG")
    if field_type == "NUMERIC":
        return "(" + "|".join(f"@{field}:[{v} {v}]" for v in values) + ")"
    return "(" + "|".join(f'@{field}:"{v}"' for v in values) + ")"


def _is_null_clause(field: str, field_type: str) -> str:
    if field_type == "NUMERIC":
        exists = f"@{field}:[-inf +inf]"
    elif field_type == "TEXT":
        exists = f"@{field}:*"
    else:
        raise ValueError(
            f"IS NULL/IS NOT NULL is only supported on NUMERIC/TEXT fields here — '{field}' is {field_type}"
        )
    return f"-({exists})"


def _like_clause(field: str, field_type: str, pattern: str) -> str:
    if field_type != "TEXT":
        raise ValueError(f"LIKE needs a TEXT field; '{field}' is {field_type}")
    starts, ends = pattern.startswith("%"), pattern.endswith("%")
    core = pattern.strip("%")
    if "_" in core:
        raise ValueError("LIKE with '_' (single-char wildcard) isn't supported by Dragonfly's search")
    if starts and not ends:
        raise ValueError(
            "LIKE with only a leading '%' (suffix match) isn't supported — "
            "use a prefix pattern ('foo%') or a full-text pattern ('%foo%') instead"
        )
    if ends and not starts:
        return f"@{field}:{core}*"
    return f'@{field}:"{core}"'


def translate_where(node: exp.Expression, field_types: dict) -> str:
    if isinstance(node, exp.Paren):
        return translate_where(node.this, field_types)
    if isinstance(node, exp.And):
        return f"({translate_where(node.this, field_types)} {translate_where(node.expression, field_types)})"
    if isinstance(node, exp.Or):
        return f"({translate_where(node.this, field_types)}|{translate_where(node.expression, field_types)})"
    if isinstance(node, exp.Not):
        return f"-({translate_where(node.this, field_types)})"

    if isinstance(node, exp.Between):
        field = field_name(node.this, field_types)
        lo, hi = literal_value(node.args["low"]), literal_value(node.args["high"])
        return build_filter_clause(field, [lo, hi], field_types[field])

    if isinstance(node, exp.In):
        field = field_name(node.this, field_types)
        values = [literal_value(v) for v in node.expressions]
        return _in_clause(field, field_types[field], values)

    if isinstance(node, exp.Is):
        if not isinstance(node.expression, exp.Null):
            raise ValueError(f"Unsupported IS expression: '{node.sql()}'")
        field = field_name(node.this, field_types)
        return _is_null_clause(field, field_types[field])

    if isinstance(node, exp.Like):
        field = field_name(node.this, field_types)
        return _like_clause(field, field_types[field], literal_value(node.expression))

    if isinstance(node, exp.EQ):
        field = field_name(node.this, field_types)
        return build_filter_clause(field, literal_value(node.expression), field_types[field])
    if isinstance(node, exp.NEQ):
        field = field_name(node.this, field_types)
        return f"-({build_filter_clause(field, literal_value(node.expression), field_types[field])})"
    if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
        field = field_name(node.this, field_types)
        if field_types[field] != "NUMERIC":
            raise ValueError(f"'{field}' is {field_types[field]} — only =/!= work on non-NUMERIC fields")
        op = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}[type(node)]
        return _numeric_bound_clause(field, op, literal_value(node.expression))

    raise ValueError(f"Unsupported WHERE expression: '{node.sql()}'")


_AGG_REDUCERS = {exp.Count: "COUNT", exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}


def select_targets(select_exprs: list, field_types: dict) -> tuple[list, list]:
    """Split a SELECT list into (plain field names, aggregate calls). Each
    aggregate call is (reducer, field_or_None, alias) — field is None for COUNT(*)."""
    plain, aggregates = [], []
    for e in select_exprs:
        alias, inner = None, e
        if isinstance(e, exp.Alias):
            alias, inner = e.alias, e.this
        if isinstance(inner, exp.Star):
            plain.append("*")
            continue
        agg_cls = next((cls for cls in _AGG_REDUCERS if isinstance(inner, cls)), None)
        if agg_cls is not None:
            reducer = _AGG_REDUCERS[agg_cls]
            arg = inner.this
            field = None if isinstance(arg, exp.Star) or arg is None else field_name(arg, field_types)
            alias = alias or (f"{reducer.lower()}_{field}" if field else reducer.lower())
            aggregates.append((reducer, field, alias))
            continue
        if isinstance(inner, exp.Column):
            plain.append(field_name(inner, field_types))
            continue
        raise ValueError(f"Unsupported SELECT expression: '{e.sql()}'")
    return plain, aggregates


def where_query_string(where_exp, schema: IndexSchema) -> str:
    return translate_where(where_exp.this, schema.field_types) if where_exp else "*"
