"""Unit tests for pingwatch.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import DataTable

import logging

import pingwatch
from pingwatch import (
    COLUMNS,
    STICKY_DURATION,
    PingResult,
    PingWatchApp,
    UrlState,
    Target,
    fmt_seconds,
    load_groups,
    read_url_groups,
    read_urls,
)


async def no_op_ping(url: str, session, sem) -> PingResult:
    async with sem:
        return make_result(url=url)


def make_result(
    url: str = "http://example.com",
    http_code: int | None = 200,
    error: str | None = None,
    total_time: float | None = 0.1,
    timestamp: datetime | None = None,
) -> PingResult:
    return PingResult(
        url=url,
        timestamp=timestamp or datetime.now(),
        connect_time=0.01,
        ttfb=0.05,
        total_time=total_time,
        http_code=http_code,
        error=error,
    )


# --- PingResult ---------------------------------------------------------


def test_ping_result_ok_when_200_and_no_error():
    assert make_result(http_code=200, error=None).ok is True


def test_ping_result_not_ok_when_non_200():
    assert make_result(http_code=404, error=None).ok is False


def test_ping_result_not_ok_when_curl_error():
    assert make_result(http_code=None, error="connection refused").ok is False


def test_ping_result_slow_above_threshold():
    assert make_result(total_time=3.5).slow is True


def test_ping_result_not_slow_at_or_below_threshold():
    assert make_result(total_time=3.0).slow is False
    assert make_result(total_time=1.0).slow is False


def test_ping_result_not_slow_when_total_time_missing():
    assert make_result(total_time=None).slow is False


# --- read_urls -----------------------------------------------------------


def test_read_urls_skips_blank_lines_and_comments(tmp_path: Path):
    path = tmp_path / "urls.txt"
    path.write_text(
        "\n".join(
            [
                "http://a.example.com",
                "",
                "# a comment",
                "  http://b.example.com  ",
                "   ",
                "http://c.example.com",
            ]
        )
    )
    assert read_urls(path) == [
        "http://a.example.com",
        "http://b.example.com",
        "http://c.example.com",
    ]


def test_read_urls_empty_file(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("")
    assert read_urls(path) == []


# --- read_url_groups / load_groups ---------------------------------------


def test_read_url_groups_parses_named_groups(tmp_path: Path):
    path = tmp_path / "urls.yaml"
    path.write_text(
        "my-group:\n"
        "  - http://a.example.com\n"
        "  - http://b.example.com\n"
        "other-group:\n"
        "  - http://c.example.com\n"
    )
    assert read_url_groups(path) == {
        "MY-GROUP": [Target("target:0:0", "http://a.example.com"), Target("target:0:1", "http://b.example.com")],
        "OTHER-GROUP": [Target("target:1:0", "http://c.example.com")],
    }


def test_read_url_groups_rejects_non_mapping(tmp_path: Path):
    path = tmp_path / "urls.yaml"
    path.write_text("- http://a.example.com\n- http://b.example.com\n")
    with pytest.raises(ValueError):
        read_url_groups(path)


def test_load_groups_dispatches_by_extension(tmp_path: Path):
    yaml_path = tmp_path / "urls.yaml"
    yaml_path.write_text("my-group:\n  - http://a.example.com\n")
    assert load_groups(yaml_path) == {"MY-GROUP": [Target("target:0:0", "http://a.example.com")]}

    txt_path = tmp_path / "urls.txt"
    txt_path.write_text("http://a.example.com\nhttp://b.example.com\n")
    assert load_groups(txt_path) == {"": [Target("target:0:0", "http://a.example.com"), Target("target:0:1", "http://b.example.com")]}


# --- fmt_seconds -----------------------------------------------------------


def test_fmt_seconds_formats_value():
    assert fmt_seconds(1.23456) == "1.235s"


def test_fmt_seconds_none_is_dash():
    assert fmt_seconds(None) == "-"


# --- UrlState.is_bad / apply (default mode: any non-200 is bad) ------------


def test_is_bad_default_mode_non_200_is_bad():
    state = UrlState(url="http://example.com")
    assert state.is_bad(make_result(http_code=500)) is True


def test_is_bad_default_mode_200_is_not_bad():
    state = UrlState(url="http://example.com")
    assert state.is_bad(make_result(http_code=200)) is False


def test_apply_sets_sticky_error_window_on_bad_result():
    state = UrlState(url="http://example.com")
    now = datetime.now()
    result = make_result(http_code=500, timestamp=now)
    is_bad = state.apply(result)

    assert is_bad is True
    assert state.ever_non_200 is True
    assert state.error_code == "500"
    assert state.error_until == now + STICKY_DURATION
    assert state.is_error(now) is True
    assert state.is_error(now + STICKY_DURATION + timedelta(seconds=1)) is False


def test_apply_sets_sticky_warning_window_on_slow_result():
    state = UrlState(url="http://example.com")
    now = datetime.now()
    result = make_result(http_code=200, total_time=5.0, timestamp=now)
    is_bad = state.apply(result)

    assert is_bad is False
    assert state.slow_rtt == 5.0
    assert state.slow_until == now + STICKY_DURATION
    assert state.is_warning(now) is True
    assert state.is_error(now) is False


def test_apply_repeated_same_error_does_not_reset_error_since():
    state = UrlState(url="http://example.com")
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1)

    state.apply(make_result(http_code=500, timestamp=t0))
    assert state.error_since == t0

    state.apply(make_result(http_code=500, timestamp=t1))
    assert state.error_since == t0  # unchanged: same error, still within sticky window
    assert state.error_until == t1 + STICKY_DURATION  # sticky window still extends


def test_apply_different_error_code_resets_error_since():
    state = UrlState(url="http://example.com")
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1)

    state.apply(make_result(http_code=500, timestamp=t0))
    state.apply(make_result(http_code=503, timestamp=t1))
    assert state.error_since == t1


def test_apply_same_error_after_sticky_window_expired_resets_error_since():
    state = UrlState(url="http://example.com")
    t0 = datetime.now()
    t1 = t0 + STICKY_DURATION + timedelta(seconds=1)

    state.apply(make_result(http_code=500, timestamp=t0))
    state.apply(make_result(http_code=500, timestamp=t1))
    assert state.error_since == t1


def test_apply_repeated_warning_does_not_reset_slow_since():
    state = UrlState(url="http://example.com")
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1)

    state.apply(make_result(total_time=5.0, timestamp=t0))
    assert state.slow_since == t0

    state.apply(make_result(total_time=6.0, timestamp=t1))
    assert state.slow_since == t0
    assert state.slow_until == t1 + STICKY_DURATION


def test_apply_tracks_longest_rtt():
    state = UrlState(url="http://example.com")
    state.apply(make_result(total_time=0.5))
    state.apply(make_result(total_time=0.2))
    state.apply(make_result(total_time=1.5))
    assert state.longest_rtt == 1.5


def test_apply_error_uses_curl_error_string_when_no_http_code():
    state = UrlState(url="http://example.com")
    state.apply(make_result(http_code=None, error="timeout"))
    assert state.error_code == "timeout"


def test_apply_respects_custom_slow_threshold():
    state = UrlState(url="http://example.com", slow_threshold_seconds=1.0)
    state.apply(make_result(total_time=1.5))
    assert state.slow_rtt == 1.5


def test_apply_respects_custom_sticky_duration():
    state = UrlState(url="http://example.com", sticky_duration=timedelta(seconds=10))
    now = datetime.now()
    state.apply(make_result(http_code=500, timestamp=now))
    assert state.error_until == now + timedelta(seconds=10)


# --- UrlState with --initial-state-normal (baseline mode) -----------------


def test_baseline_mode_first_ping_sets_baseline_and_is_not_bad():
    state = UrlState(url="http://example.com", initial_state_normal=True)
    is_bad = state.apply(make_result(http_code=404))

    assert is_bad is False
    assert state.baseline_set is True
    assert state.baseline_code == 404
    assert state.ever_non_200 is False


def test_baseline_mode_deviation_from_baseline_is_bad():
    state = UrlState(url="http://example.com", initial_state_normal=True)
    state.apply(make_result(http_code=404))  # establishes baseline of 404

    is_bad = state.apply(make_result(http_code=200))
    assert is_bad is True


def test_baseline_mode_matching_baseline_is_not_bad():
    state = UrlState(url="http://example.com", initial_state_normal=True)
    state.apply(make_result(http_code=404))  # establishes baseline of 404

    is_bad = state.apply(make_result(http_code=404))
    assert is_bad is False


def test_baseline_mode_curl_error_after_baseline_is_bad():
    state = UrlState(url="http://example.com", initial_state_normal=True)
    state.apply(make_result(http_code=200))

    is_bad = state.apply(make_result(http_code=None, error="timeout"))
    assert is_bad is True


# --- PingWatchApp runtime settings (command palette callbacks) -------------


@pytest.mark.asyncio
async def test_apply_slow_threshold_propagates_to_all_states(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com", "http://b.example.com"])
    async with app.run_test():
        app._apply_slow_threshold(9.5)
        assert app.slow_threshold_seconds == 9.5
        assert all(state.slow_threshold_seconds == 9.5 for state in app.states.values())


@pytest.mark.asyncio
async def test_apply_slow_threshold_ignores_none(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com"])
    async with app.run_test():
        original = app.slow_threshold_seconds
        app._apply_slow_threshold(None)
        assert app.slow_threshold_seconds == original


@pytest.mark.asyncio
async def test_apply_sticky_duration_propagates_to_all_states(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com", "http://b.example.com"])
    async with app.run_test():
        app._apply_sticky_duration(30.0)
        assert app.sticky_duration == timedelta(seconds=30)
        assert all(state.sticky_duration == timedelta(seconds=30) for state in app.states.values())


@pytest.mark.asyncio
async def test_apply_sticky_duration_ignores_none(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com"])
    async with app.run_test():
        original = app.sticky_duration
        app._apply_sticky_duration(None)
        assert app.sticky_duration == original


@pytest.mark.asyncio
async def test_apply_rtt_window_propagates_to_all_states(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com", "http://b.example.com"])
    async with app.run_test():
        app._apply_rtt_window(120.0)
        assert app.rtt_window == timedelta(seconds=120)
        assert all(state.rtt_window == timedelta(seconds=120) for state in app.states.values())


@pytest.mark.asyncio
async def test_apply_rtt_window_ignores_none(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    app = make_app(["http://a.example.com"])
    async with app.run_test():
        original = app.rtt_window
        app._apply_rtt_window(None)
        assert app.rtt_window == original


# --- PingWatchApp.row_sort_key / reorder_rows -------------------------------


def target_id(app: PingWatchApp, url: str) -> str:
    return next(target.id for target in app.targets.values() if target.url == url)


def state_for(app: PingWatchApp, url: str) -> UrlState:
    return app.states[target_id(app, url)]


def sorted_urls(app: PingWatchApp) -> list[str]:
    ids = sorted(app.targets, key=lambda key: app.row_sort_key(app.url_cell(key)))
    return [app.targets[key].url for key in ids]


def url_cell(app: PingWatchApp, url: str) -> Text:
    return app.url_cell(target_id(app, url))


def group_cell(name: str) -> Text:
    return Text(name, style=Style(meta={"group": name}))


def make_app(urls: list[str]) -> PingWatchApp:
    logger = logging.getLogger("pingwatch-test")
    return PingWatchApp({"": urls}, interval=1.0, logger=logger)


def test_row_sort_key_orders_error_before_warning_before_normal():
    app = make_app(["http://err.example.com", "http://warn.example.com", "http://ok.example.com"])
    now = datetime.now()

    state_for(app, "http://err.example.com").apply(make_result(url="http://err.example.com", http_code=500, timestamp=now))
    state_for(app, "http://warn.example.com").apply(make_result(url="http://warn.example.com", total_time=5.0, timestamp=now))
    state_for(app, "http://ok.example.com").apply(make_result(url="http://ok.example.com", timestamp=now))

    keys = {
        url: app.row_sort_key(url_cell(app, url))
        for url in ["http://err.example.com", "http://warn.example.com", "http://ok.example.com"]
    }
    ordered = sorted(keys, key=lambda u: keys[u])
    assert ordered == ["http://err.example.com", "http://warn.example.com", "http://ok.example.com"]


def test_row_sort_key_normal_urls_sorted_alphabetically():
    app = make_app(["http://zeta.example.com", "http://alpha.example.com", "http://beta.example.com"])
    for url in [target.url for target in app.targets.values()]:
        state_for(app, url).apply(make_result(url=url))

    ordered = sorted_urls(app)
    assert ordered == [
        "http://alpha.example.com",
        "http://beta.example.com",
        "http://zeta.example.com",
    ]


def test_row_sort_key_most_recent_error_sorts_first():
    app = make_app(["http://old-error.example.com", "http://new-error.example.com"])
    older = datetime.now() - timedelta(seconds=30)
    newer = datetime.now()

    state_for(app, "http://old-error.example.com").apply(
        make_result(url="http://old-error.example.com", http_code=500, timestamp=older)
    )
    state_for(app, "http://new-error.example.com").apply(
        make_result(url="http://new-error.example.com", http_code=500, timestamp=newer)
    )

    ordered = sorted_urls(app)
    assert ordered == ["http://new-error.example.com", "http://old-error.example.com"]


def test_row_sort_key_most_recent_warning_sorts_first():
    app = make_app(["http://old-warn.example.com", "http://new-warn.example.com"])
    older = datetime.now() - timedelta(seconds=30)
    newer = datetime.now()

    state_for(app, "http://old-warn.example.com").apply(
        make_result(url="http://old-warn.example.com", total_time=5.0, timestamp=older)
    )
    state_for(app, "http://new-warn.example.com").apply(
        make_result(url="http://new-warn.example.com", total_time=5.0, timestamp=newer)
    )

    ordered = sorted_urls(app)
    assert ordered == ["http://new-warn.example.com", "http://old-warn.example.com"]


def test_row_sort_key_repeated_same_error_keeps_previous_rank():
    app = make_app(["http://first-error.example.com", "http://second-error.example.com"])
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1)
    t2 = t0 + timedelta(seconds=2)

    # first-error errors first, so it ranks above second-error.
    state_for(app, "http://first-error.example.com").apply(
        make_result(url="http://first-error.example.com", http_code=500, timestamp=t0)
    )
    state_for(app, "http://second-error.example.com").apply(
        make_result(url="http://second-error.example.com", http_code=500, timestamp=t1)
    )
    ordered = sorted_urls(app)
    assert ordered == ["http://second-error.example.com", "http://first-error.example.com"]

    # first-error keeps erroring with the SAME code on a later ping -- it should NOT
    # jump back above second-error just because it was pinged again.
    state_for(app, "http://first-error.example.com").apply(
        make_result(url="http://first-error.example.com", http_code=500, timestamp=t2)
    )
    ordered = sorted_urls(app)
    assert ordered == ["http://second-error.example.com", "http://first-error.example.com"]


def test_row_sort_key_different_error_code_does_move_to_top():
    app = make_app(["http://first-error.example.com", "http://second-error.example.com"])
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1)
    t2 = t0 + timedelta(seconds=2)

    state_for(app, "http://first-error.example.com").apply(
        make_result(url="http://first-error.example.com", http_code=500, timestamp=t0)
    )
    state_for(app, "http://second-error.example.com").apply(
        make_result(url="http://second-error.example.com", http_code=500, timestamp=t1)
    )

    # first-error now returns a *different* error code -- this is a genuinely new
    # error, so it should jump back to the top.
    state_for(app, "http://first-error.example.com").apply(
        make_result(url="http://first-error.example.com", http_code=503, timestamp=t2)
    )
    ordered = sorted_urls(app)
    assert ordered == ["http://first-error.example.com", "http://second-error.example.com"]


def test_row_sort_key_uses_identity_independent_of_display_text():
    app = make_app(["http://example.com"])
    cell = url_cell(app, "http://example.com")
    original_key = app.row_sort_key(cell)
    cell.plain = "a different display label"
    assert app.row_sort_key(cell) == original_key


def make_grouped_app(groups: dict[str, list[str]]) -> PingWatchApp:
    logger = logging.getLogger("pingwatch-test")
    return PingWatchApp(groups, interval=1.0, logger=logger)


def test_row_sort_key_keeps_groups_contiguous_even_with_cross_group_error():
    # An error in the second group must not outrank the first group's rows.
    app = make_grouped_app(
        {
            "first-group": ["http://a.example.com", "http://b.example.com"],
            "second-group": ["http://err.example.com"],
        }
    )
    for url in ["http://a.example.com", "http://b.example.com"]:
        state_for(app, url).apply(make_result(url=url))
    state_for(app, "http://err.example.com").apply(make_result(url="http://err.example.com", http_code=500))

    cells = [group_cell("first-group"), url_cell(app, "http://a.example.com"), url_cell(app, "http://b.example.com"), group_cell("second-group"), url_cell(app, "http://err.example.com")]
    ordered = sorted(cells, key=lambda c: app.row_sort_key(c))
    assert [c.plain for c in ordered] == [
        "first-group",
        "http://a.example.com",
        "http://b.example.com",
        "second-group",
        "http://err.example.com",
    ]


def test_row_sort_key_heading_sorts_before_its_group_errors():
    app = make_grouped_app({"my-group": ["http://err.example.com", "http://ok.example.com"]})
    state_for(app, "http://err.example.com").apply(make_result(url="http://err.example.com", http_code=500))
    state_for(app, "http://ok.example.com").apply(make_result(url="http://ok.example.com"))

    cells = [url_cell(app, "http://err.example.com"), url_cell(app, "http://ok.example.com"), group_cell("my-group")]
    ordered = sorted(cells, key=lambda c: app.row_sort_key(c))
    assert [c.plain for c in ordered] == ["my-group", "http://err.example.com", "http://ok.example.com"]


# --- Integration: DataTable actually reorders rows --------------------------


@pytest.mark.asyncio
async def test_reorder_rows_moves_new_error_to_top(monkeypatch):
    monkeypatch.setattr(pingwatch, "ping", no_op_ping)
    urls = ["http://alpha.example.com", "http://beta.example.com", "http://gamma.example.com"]
    app = make_app(urls)

    async with app.run_test():
        table = app.query_one(DataTable)

        def visible_urls() -> list[str]:
            column_key = app._column_keys["URL"]
            return [table.get_cell(table.ordered_rows[i].key, column_key).plain for i in range(len(urls))]

        # initial mount order is insertion order (no state yet -> all "normal", alphabetical)
        assert visible_urls() == [
            "http://alpha.example.com",
            "http://beta.example.com",
            "http://gamma.example.com",
        ]

        # gamma becomes an error -> should jump to the top
        state_for(app, "http://gamma.example.com").apply(make_result(url="http://gamma.example.com", http_code=500))
        app.render_row(target_id(app, "http://gamma.example.com"))
        app.reorder_rows()

        assert visible_urls()[0] == "http://gamma.example.com"

        # beta becomes a warning -> should land right after the error, above alpha
        state_for(app, "http://beta.example.com").apply(make_result(url="http://beta.example.com", total_time=5.0))
        app.render_row(target_id(app, "http://beta.example.com"))
        app.reorder_rows()

        assert visible_urls() == [
            "http://gamma.example.com",
            "http://beta.example.com",
            "http://alpha.example.com",
        ]

        # alpha now errors too, so it should jump above the (older) gamma error.
        state_for(app, "http://alpha.example.com").apply(make_result(url="http://alpha.example.com", http_code=500))
        app.render_row(target_id(app, "http://alpha.example.com"))
        app.reorder_rows()

        assert visible_urls() == [
            "http://alpha.example.com",
            "http://gamma.example.com",
            "http://beta.example.com",
        ]

        # gamma errors again with the SAME code -- it must NOT jump back above alpha.
        state_for(app, "http://gamma.example.com").apply(make_result(url="http://gamma.example.com", http_code=500))
        app.render_row(target_id(app, "http://gamma.example.com"))
        app.reorder_rows()

        assert visible_urls() == [
            "http://alpha.example.com",
            "http://gamma.example.com",
            "http://beta.example.com",
        ]
