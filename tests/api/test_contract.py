"""Contract tests and fuzzing generated from the OpenAPI spec with Schemathesis.

Schemathesis builds requests from the spec (valid ones and deliberately broken
ones) and checks that every response matches what the spec promises: status
codes, content types and body schemas, and never a 5xx.
"""

from __future__ import annotations

import itertools

import pytest
import schemathesis
from hypothesis import HealthCheck, settings

from app.seed import ADMIN_KEY, user_key
from tests.api.conftest import EXTRA_ADMIN_KEYS


@pytest.fixture(scope="session")
def api_schema(base_url: str):
    return schemathesis.openapi.from_url(f"{base_url}/openapi.json")


schema = schemathesis.pytest.from_fixture("api_schema")


@schemathesis.hook
def filter_query(ctx, query) -> bool:
    # Skip requests that add query parameters the spec never declared. Like most
    # APIs, this one ignores unknown query parameters rather than rejecting them.
    if not query:
        return True
    declared = {param.name for param in ctx.operation.query}
    return set(query) <= declared


# Spread generated requests over many keys so the rate limiter doesn't turn
# most of them into 429s. Users 60-99 are left alone by the other tests.
_user_keys = itertools.cycle([user_key(n) for n in range(60, 100)])
_admin_keys = itertools.cycle([ADMIN_KEY, *EXTRA_ADMIN_KEYS])


@schema.parametrize()
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_api_matches_its_contract(case) -> None:
    key = next(_admin_keys) if case.path.startswith("/admin") else next(_user_keys)
    case.call_and_validate(headers={"X-API-Key": key})
