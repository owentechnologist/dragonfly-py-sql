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
