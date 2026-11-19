from __future__ import annotations

from turf_app.ratelimit import RateLimiter, Rule


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limiter(clock: FakeClock, max_tracked: int = 100) -> RateLimiter:
    rules = (
        Rule("/api/targets", limit=2, window_s=60, method="POST"),
        Rule("/api/", limit=3, window_s=10),
    )
    return RateLimiter(rules, clock=clock, max_tracked=max_tracked)


def test_allows_up_to_limit_then_blocks_with_retry_after() -> None:
    clock = FakeClock()
    rl = limiter(clock)
    assert [rl.check("1.1.1.1", "GET", "/api/meta") for _ in range(3)] == [None, None, None]
    clock.now += 4
    assert rl.check("1.1.1.1", "GET", "/api/meta") == 6  # oldest hit expires in 6 s


def test_window_slides() -> None:
    clock = FakeClock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("ip", "GET", "/api/meta")
    clock.now += 10
    assert rl.check("ip", "GET", "/api/meta") is None


def test_clients_are_independent() -> None:
    clock = FakeClock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a", "GET", "/api/meta")
    assert rl.check("a", "GET", "/api/meta") is not None
    assert rl.check("b", "GET", "/api/meta") is None


def test_specific_rule_applies_by_method() -> None:
    clock = FakeClock()
    rl = limiter(clock)
    assert rl.check("ip", "POST", "/api/targets/export") is None
    assert rl.check("ip", "POST", "/api/targets") is None
    assert rl.check("ip", "POST", "/api/targets") is not None  # POST limit is 2
    # A GET on the same path falls through to the general /api/ rule.
    assert rl.check("ip", "GET", "/api/targets") is None


def test_rules_have_separate_budgets() -> None:
    clock = FakeClock()
    rl = limiter(clock)
    for _ in range(2):
        rl.check("ip", "POST", "/api/targets")
    assert rl.check("ip", "GET", "/api/meta") is None


def test_unmatched_paths_are_not_limited() -> None:
    rl = limiter(FakeClock())
    assert all(rl.check("ip", "GET", "/healthz") is None for _ in range(100))


def test_memory_is_bounded() -> None:
    rl = limiter(FakeClock(), max_tracked=10)
    for i in range(25):
        rl.check(f"10.0.0.{i}", "GET", "/api/meta")
    assert len(rl._hits) <= 10
