"""Minimal Redis client factory for talking to a Dragonfly instance."""

import redis


def make_client(
    host: str = "localhost",
    port: int = 6379,
    db: int = 0,
    username: str | None = None,
    password: str | None = None,
    uri: str | None = None,
    **kwargs,
) -> redis.Redis:
    """Build a client either from a connection URI or discrete host/port args.

    `uri` takes the form `redis://[[username]:[password]@]host[:port][/db]`
    (also accepts `rediss://` for TLS) and, when given, overrides every other
    connection argument.
    """
    if uri:
        return redis.Redis.from_url(uri, decode_responses=True, **kwargs)
    return redis.Redis(
        host=host,
        port=port,
        db=db,
        username=username,
        password=password,
        decode_responses=True,
        **kwargs,
    )
