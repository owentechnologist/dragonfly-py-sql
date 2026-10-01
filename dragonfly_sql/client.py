"""Minimal Redis client factory for talking to a Dragonfly instance."""

import redis


def make_client(
    host: str = "localhost",
    port: int = 6379,
    db: int = 0,
    username: str | None = None,
    password: str | None = None,
    **kwargs,
) -> redis.Redis:
    return redis.Redis(
        host=host,
        port=port,
        db=db,
        username=username,
        password=password,
        decode_responses=True,
        **kwargs,
    )
