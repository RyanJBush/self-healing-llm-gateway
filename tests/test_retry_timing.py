"""A timer waking early must not shorten the configured minimum retry delay."""

import asyncio

import pytest

from gateway.providers import http_error, ok
from helpers import make_rig, post, run


@pytest.mark.parametrize("retry_after,minimum", [(None, 0.1), ("1", 1.0)])
def test_retry_rechecks_minimum_delay_after_an_early_wakeup(retry_after, minimum):
    sleeps = []

    async def wakes_early_once(seconds):
        sleeps.append(seconds)
        # Reproduce the 6 ms early timer observed on Windows/Python 3.11 CI.
        await asyncio.sleep(seconds - 0.006 if len(sleeps) == 1 else seconds)

    rig = make_rig(
        primary=[http_error(503, retry_after=retry_after), ok()],
        sleep=wakes_early_once,
    )
    response = run(post(rig))
    assert response.status == 200 and response.json["attempts"] == 2
    assert rig.primary.call_times[1] - rig.primary.call_times[0] >= minimum - 1e-9
    assert response.json["elapsed_ms"] == round(minimum * 1000)
    assert len(sleeps) == 2
