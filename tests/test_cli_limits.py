"""The limits of the HTTP API can be set on the command line, and without them the defaults are the ones the API always had."""

from __future__ import annotations

import pytest

from hydra_umc_datalake import main as main_module
from hydra_umc_datalake.api import DEFAULT_MAX_REQUEST_BODY_BYTES, DEFAULT_REQUEST_TIMEOUT_SECONDS



def _capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    class FakeServer:
        def __init__(self, address, store, **options) -> None:
            seen["options"] = options

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "DatalakeServer", FakeServer)
    return seen


def test_without_flags_the_limits_are_the_ones_the_api_always_had(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    assert main_module.main(["--db", ":memory:"]) == 0
    assert seen["options"]["max_request_body_bytes"] == DEFAULT_MAX_REQUEST_BODY_BYTES == 1_048_576
    assert seen["options"]["request_timeout_seconds"] == DEFAULT_REQUEST_TIMEOUT_SECONDS == 15.0


def test_the_flags_reach_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    assert main_module.main(["--db", ":memory:", "--max-request-body-bytes", "4096", "--request-timeout-seconds", "2.5"]) == 0
    assert seen["options"]["max_request_body_bytes"] == 4096
    assert seen["options"]["request_timeout_seconds"] == 2.5


@pytest.mark.parametrize(
    "flags",
    [["--max-request-body-bytes", "0"], ["--max-request-body-bytes", "-5"], ["--request-timeout-seconds", "0"], ["--request-timeout-seconds", "-1"]],
)
def test_nonsense_limits_stop_the_start(monkeypatch: pytest.MonkeyPatch, flags: list[str]) -> None:
    _capture(monkeypatch)
    assert main_module.main(["--db", ":memory:", *flags]) == 2
