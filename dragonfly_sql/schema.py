"""Schema introspection for Dragonfly FT.* search indexes.

Parses FT.INFO's flat [key, value, key, value, ...] reply into an IndexSchema
and answers the two questions writes need that reads don't: what key prefix
new rows belong under, and what a field's default value should be when an
INSERT doesn't supply one. Fields come back in sorted (case-insensitive
alphabetical) order regardless of the order FT.INFO listed its attributes in,
so DESCRIBE, SELECT * and INSERT all produce deterministic output.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexField:
    name: str
    type: str
    # JSONPath for a JSON index (e.g. "$.brand"); identical to `name` for HASH.
    path: str


@dataclass(frozen=True)
class IndexSchema:
    name: str
    key_type: str  # "HASH" or "JSON"
    prefix: str  # first declared key prefix; new rows are created under it
    fields: list[IndexField]
    num_docs: int | None = None

    @property
    def field_types(self) -> dict[str, str]:
        return {f.name: f.type for f in self.fields}

    def field(self, name: str) -> IndexField:
        for f in self.fields:
            if f.name.lower() == name.lower():
                return f
        raise ValueError(
            f"'{name}' is not a field on index '{self.name}'. "
            f"Available fields: {sorted(f.name for f in self.fields)}"
        )


# GEO/VECTOR/GEOSHAPE have no safe generic default (unit, dimension,
# projection all vary per index), so those fields are simply left unset on
# INSERT unless the statement supplies them explicitly.
_DEFAULTS_BY_TYPE = {"TAG": "", "TEXT": "", "NUMERIC": 0}


def default_value(field_type: str):
    return _DEFAULTS_BY_TYPE.get(field_type)


_NUMERIC_TYPES = {"NUMERIC"}
_STRING_TYPES = {"TAG", "TEXT"}


def coerce_value(value, field_type: str | None):
    """Convert a value read back from Dragonfly into the Python type its
    FT.INFO-declared field type implies. The wire reply is sometimes already
    typed (Dragonfly replies with real doubles for NUMERIC fields) and
    sometimes a plain string (HGETALL, a plain Redis/RediSearch backend) --
    this normalizes either case so callers get a value of the same type no
    matter which one showed up."""
    if value is None:
        return None
    if field_type in _NUMERIC_TYPES:
        return float(value)
    if field_type in _STRING_TYPES:
        return str(value)
    return value


def _to_dict(raw) -> dict:
    return dict(zip(raw[0::2], raw[1::2])) if isinstance(raw, list) else (raw or {})


def parse_ft_info(raw: list | dict) -> IndexSchema:
    top = _to_dict(raw)
    definition = _to_dict(top.get("index_definition"))

    fields = []
    for spec_raw in top.get("attributes", []) or []:
        spec = _to_dict(spec_raw)
        fields.append(
            IndexField(
                name=spec.get("attribute") or spec.get("identifier"),
                type=spec.get("type"),
                path=spec.get("identifier"),
            )
        )

    prefixes = definition.get("prefixes") or []
    prefix = prefixes[0] if prefixes else f"{top.get('index_name')}:"
    num_docs = top.get("num_docs")

    # FT.INFO's attribute order is not stable run to run, so sort here -- the
    # one place fields are built -- to keep every consumer's output deterministic.
    fields.sort(key=lambda f: f.name.lower())

    return IndexSchema(
        name=top.get("index_name"),
        key_type=definition.get("key_type"),
        prefix=prefix,
        fields=fields,
        num_docs=int(num_docs) if num_docs is not None else None,
    )


def fetch_schema(client, table: str) -> IndexSchema:
    try:
        raw = client.execute_command("FT.INFO", table)
    except Exception as e:
        raise ValueError(f"Unknown table '{table}' ({e}). Run SHOW TABLES to see available tables.")
    return parse_ft_info(raw)
