"""In-memory rate limiter middleware.

Limits the 4 admin routers (under /api/v1/admin) to 300 requests/minute
per authenticated user. Sliding window with deque per user_id.

This is in-memory and resets on API restart. Acceptable for a single
deploy target; if we ever go multi-instance, swap to Redis-backed.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


ADMIN_PATH_PREFIX = "/api/v1/admin"
REQUESTS_PER_MINUTE = 300
WINDOW_SECONDS = 60


class AdminRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-user 300 req/min sliding window for admin endpoints.

    Unauthenticated requests are NOT limited here (auth middleware returns
    401 first). Keyed on the JWT user id if available, else IP.
    """

    def __init__(self, app, requests_per_minute: int = REQUESTS_PER_MINUTE):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not request.url.path.startswith(ADMIN_PATH_PREFIX):
            return await call_next(request)

        key = self._key_from_request(request)
        now = time.monotonic()
        bucket = self._buckets[key]
        cutoff = now - WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.requests_per_minute:
            return Response(
                content='{"detail":"rate limit exceeded for admin endpoints"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )

        bucket.append(now)
        response = await call_next(request)
        return response

    @staticmethod
    def _key_from_request(request: Request) -> str:
        user = getattr(request.state, "user", None)
        if user is not None and getattr(user, "id", None) is not None:
            return f"user:{user.id}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"
