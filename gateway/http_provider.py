"""A provider that really speaks HTTP (httpx) to a trusted, operator-configured URL.

Used for the local timeout/cancellation integration test and as the shape a real provider
would take. SDK retries and transport retries are disabled: the gateway owns retries.
Request IDs are sent only as a correlation header, never as an idempotency key.
"""

from __future__ import annotations

import httpx

from .providers import CallContext, ConnectionFailure, RawResponse

_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


class HttpProvider:
    execution_mode = "real"

    def __init__(self, provider_id: str, model: str, url: str):
        self.id = provider_id
        self.model = model
        self._url = url
        # retries=0: no hidden transport retries. No httpx-level timeout either: the
        # gateway applies the per-call timeout and cancels this coroutine, which closes
        # the connection.
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            timeout=None,
            follow_redirects=False,
        )

    async def call(self, text: str, ctx: CallContext) -> RawResponse:
        try:
            resp = await self._client.post(
                self._url,
                json={"model": self.model, "input": text},
                headers={"x-correlation-id": ctx.request_id},
            )
        except _TRANSPORT_ERRORS as exc:
            raise ConnectionFailure(type(exc).__name__) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        return RawResponse(resp.status_code, dict(resp.headers), body)

    async def aclose(self) -> None:
        await self._client.aclose()
