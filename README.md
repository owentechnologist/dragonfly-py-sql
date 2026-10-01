# dragonfly-py-sql
A dragonfly SQL driver that assumes the use of SEARCH indexes against either JSON or HASH data types

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and execution.

```bash
uv sync
```

## Running the end-to-end test

Requires a running Dragonfly instance (defaults to `localhost:6379`; override with
`DRAGONFLY_HOST`/`DRAGONFLY_PORT`):

```bash
uv run test_driver.py
```

To connect elsewhere, pass a connection URI with `-U`/`--uri` (or set
`DRAGONFLY_URI`) instead:

```bash
uv run test_driver.py -U redis://user:pass@host:6379/0
```

The URI form is `redis://[[username]:[password]@]host[:port][/db]` (use
`rediss://` for TLS) and takes precedence over `DRAGONFLY_HOST`/`DRAGONFLY_PORT`.

## Connecting from your own code

`make_client` accepts either a URI or discrete host/port arguments:

```python
from dragonfly_sql.client import make_client

client = make_client(uri="redis://user:pass@host:6379/0")
# or
client = make_client(host="localhost", port=6379)
```
