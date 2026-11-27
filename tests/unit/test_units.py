"""Unit tests for config, cache and rate limiter. No database needed."""

from __future__ import annotations

import hashlib
import threading
import time

import pytest

from app.cache import TTLCache
from app.config import KNOWN_BUGS, Settings, parse_bugs
from app.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ----------------------------------------------------------------- config


def test_no_bugs_by_default() -> None:
    assert parse_bugs(None) == frozenset()
    assert parse_bugs("  ") == frozenset()


def test_bug_list_is_parsed_and_normalised() -> None:
    assert parse_bugs("Race, n_plus_one ,") == {"race", "n_plus_one"}


def test_all_switches_on_every_bug() -> None:
    assert parse_bugs("all") == set(KNOWN_BUGS)


def test_unknown_bug_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="slow_disk"):
        parse_bugs("race,slow_disk")


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERF_BUGS", "race")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "7")
    monkeypatch.setenv("STATS_TTL_SECONDS", "1.5")
    settings = Settings.from_env()
    assert settings.has("race") and not settings.has("n_plus_one")
    assert settings.rate_limit_per_minute == 7
    assert settings.stats_ttl_seconds == 1.5


def test_has_rejects_typos() -> None:
    with pytest.raises(KeyError):
        Settings().has("rcae")


def test_with_overrides_returns_a_copy() -> None:
    base = Settings()
    changed = base.with_overrides(payment_delay_ms=0)
    assert changed.payment_delay_ms == 0 and base.payment_delay_ms == 50


# ------------------------------------------------------------------ cache


@pytest.mark.parametrize("single_flight", [True, False])
def test_cache_hits_until_ttl_expires(single_flight: bool) -> None:
    clock = FakeClock()
    cache: TTLCache[int] = TTLCache(10, single_flight=single_flight, clock=clock)
    calls = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert cache.get_or_compute("k", compute) == 1
    clock.now += 9
    assert cache.get_or_compute("k", compute) == 1
    clock.now += 2
    assert cache.get_or_compute("k", compute) == 2
    cache.clear()
    assert cache.get_or_compute("k", compute) == 3


def _concurrent_misses(single_flight: bool, callers: int = 12) -> int:
    cache: TTLCache[str] = TTLCache(60, single_flight=single_flight)
    computed = []
    start = threading.Barrier(callers)

    def compute() -> str:
        computed.append(1)
        time.sleep(0.05)
        return "report"

    def caller() -> None:
        start.wait()
        assert cache.get_or_compute("hot", compute) == "report"

    threads = [threading.Thread(target=caller) for _ in range(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(computed)


def test_single_flight_computes_once_for_concurrent_misses() -> None:
    assert _concurrent_misses(single_flight=True) == 1


def test_without_single_flight_concurrent_misses_stampede() -> None:
    assert _concurrent_misses(single_flight=False) > 1


def test_single_flight_keys_do_not_block_each_other() -> None:
    cache: TTLCache[str] = TTLCache(60, single_flight=True)
    assert cache.get_or_compute("a", lambda: "A") == "A"
    assert cache.get_or_compute("b", lambda: "B") == "B"


# ----------------------------------------------------------- rate limiter


def test_rate_limiter_allows_a_burst_then_refuses() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, clock=clock)
    assert [limiter.check("k") for _ in range(3)] == [None, None, None]
    wait = limiter.check("k")
    assert wait is not None and 0 < wait <= 20


def test_rate_limiter_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, clock=clock)
    for _ in range(60):
        assert limiter.check("k") is None
    assert limiter.check("k") is not None
    clock.now += 1  # one token per second at 60/min
    assert limiter.check("k") is None


def test_rate_limiter_keys_are_independent() -> None:
    limiter = RateLimiter(per_minute=1, clock=FakeClock())
    assert limiter.check("a") is None
    assert limiter.check("a") is not None
    assert limiter.check("b") is None


def test_rate_limit_zero_means_off() -> None:
    limiter = RateLimiter(per_minute=0)
    assert not limiter.enabled
    assert all(limiter.check("k") is None for _ in range(1000))


@pytest.mark.parametrize(("seconds", "header"), [(0.01, "1"), (1.0, "1"), (1.2, "2")])
def test_retry_after_rounds_up_to_whole_seconds(seconds: float, header: str) -> None:
    assert RateLimiter.retry_after_header(seconds) == header


def test_rate_limiter_peek_takes_no_token() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=2, clock=clock)
    assert [limiter.peek("k") for _ in range(5)] == [None] * 5  # peeking spends nothing
    limiter.check("k")
    limiter.check("k")
    wait = limiter.peek("k")
    assert wait is not None and 0 < wait <= 30
    clock.now += 30  # one token back at 2/min
    assert limiter.peek("k") is None
    assert RateLimiter(per_minute=0).peek("k") is None  # off


def test_security_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_FAILURES_PER_MINUTE", "5")
    monkeypatch.setenv("MAX_SEATS_PER_BUYER", "4")
    settings = Settings.from_env()
    assert (settings.auth_failures_per_minute, settings.max_seats_per_buyer_per_event) == (5, 4)
    defaults = Settings()
    assert (defaults.auth_failures_per_minute, defaults.max_seats_per_buyer_per_event) == (30, 10)


def test_keys_hash_to_sha256_hex() -> None:
    from app.keys import hash_key

    assert hash_key("key-user-1") == hashlib.sha256(b"key-user-1").hexdigest()
    assert len(hash_key("x")) == 64
