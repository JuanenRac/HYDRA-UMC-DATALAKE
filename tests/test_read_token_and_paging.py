# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_read_token_and_paging.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The option that insists on a read token, and a seeded property test of the paging by cursor."""
from __future__ import annotations

import random

import pytest

from hydra_umc_datalake.main import read_is_open, startup_problem
from hydra_umc_datalake.store import Sample, TimeSeriesStore

TOKEN = "t" * 20
FULL = {"ingest": TOKEN, "admin": TOKEN}


@pytest.mark.parametrize(
    ("addr", "tokens", "identity", "open_"),
    [
        ("127.0.0.1", {}, False, False),  # loopback is never "open to the network"
        ("0.0.0.0", {}, False, True),
        ("0.0.0.0", FULL, False, True),  # write and admin tokens do not close reading
        ("0.0.0.0", {**FULL, "read": TOKEN}, False, False),
        ("0.0.0.0", {}, True, False),  # the identity of the SERVER closes every route
    ],
)
def test_reading_is_open_only_without_a_read_token_off_loopback(addr: str, tokens: dict[str, str], identity: bool, open_: bool) -> None:
    assert read_is_open(addr, tokens, identity) is open_


def test_the_option_turns_open_reading_into_a_refusal_and_is_off_by_default() -> None:
    # off: reading may stay open, as it always was
    assert startup_problem("0.0.0.0", FULL, False) is None
    assert startup_problem("0.0.0.0", FULL, False, False, False) is None
    # on: a read token is needed off loopback
    assert "token for reading" in str(startup_problem("0.0.0.0", FULL, False, False, True))
    assert startup_problem("0.0.0.0", {**FULL, "read": TOKEN}, False, False, True) is None
    # on, but loopback or the identity of the SERVER: nothing is open, nothing to refuse
    assert startup_problem("127.0.0.1", {}, False, False, True) is None
    assert startup_problem("0.0.0.0", {}, False, True, True) is None


def test_the_refusal_for_missing_write_tokens_still_comes_first() -> None:
    assert "ingest and admin" in str(startup_problem("0.0.0.0", {}, False, False, True))


def _filled_store(seed: int, count: int) -> tuple[TimeSeriesStore, int]:
    rng = random.Random(seed)
    store = TimeSeriesStore(":memory:")
    base = 1_800_000_000_000
    samples = []
    for _ in range(count):
        # few distinct timestamps, sources and fields: ties everywhere, which is where paging goes wrong
        samples.append(
            Sample(
                source_id=rng.choice(["a", "b", "c"]),
                kind=rng.choice(["temp", "load"]),
                timestamp=base + rng.randrange(0, 6),
                fields={rng.choice(["j1", "j2"]): float(rng.randrange(100))},
            )
        )
    store.insert_many(samples)
    everything, _ = store.query_page(limit=10_000)
    return store, len(everything)


def _walk(store: TimeSeriesStore, limit: int, descending: bool) -> list[tuple[str, str, str, int, float]]:
    seen: list[tuple[str, str, str, int, float]] = []
    after = None
    for _ in range(10_000):  # a bound, so a cursor that never ends fails instead of hanging
        points, after = store.query_page(limit=limit, after=after, descending=descending)
        seen.extend((p.source_id, p.kind, p.field, p.timestamp, p.value) for p in points)
        if after is None:
            return seen
    raise AssertionError("the cursor never ended")


@pytest.mark.parametrize("seed", [1234, 99, 7])
@pytest.mark.parametrize("limit", [1, 2, 5, 17, 1000])
def test_walking_every_page_gives_every_row_once_in_order_in_both_directions(seed: int, limit: int) -> None:
    store, total = _filled_store(seed, 120)
    whole, no_cursor = store.query_page(limit=10_000)
    assert no_cursor is None and len(whole) == total
    expected = [(p.source_id, p.kind, p.field, p.timestamp, p.value) for p in whole]

    forward = _walk(store, limit, descending=False)
    assert forward == expected  # same rows, same order, none twice, none lost
    backward = _walk(store, limit, descending=True)
    assert backward == list(reversed(expected))
