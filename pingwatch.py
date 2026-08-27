#!/usr/bin/env python3
"""Ping a list of URLs concurrently and show live status in a terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from rich.text import Text
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

SLOW_THRESHOLD_SECONDS = 3
STICKY_DURATION = timedelta(minutes=5)
DEFAULT_RTT_WINDOW_SECONDS = 60


@dataclass
class PingResult:
    url: str
    timestamp: datetime
    connect_time: float | None
    ttfb: float | None
    total_time: float | None
    http_code: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.http_code == 200

    @property
    def slow(self) -> bool:
        return self.is_slow(SLOW_THRESHOLD_SECONDS)

    def is_slow(self, threshold: float) -> bool:
        return self.total_time is not None and self.total_time > threshold


CURL_FORMAT = "%{time_connect}|%{time_starttransfer}|%{time_total}|%{http_code}"


async def ping(url: str, sem: asyncio.Semaphore) -> PingResult:
    now = datetime.now()
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-o", "/dev/null", "-s", "-w", CURL_FORMAT, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return PingResult(url, now, None, None, None, None, error=stderr.decode().strip() or "curl error")

    try:
        connect_s, ttfb_s, total_s, code_s = stdout.decode().strip().split("|")
        return PingResult(url, now, float(connect_s), float(ttfb_s), float(total_s), int(code_s))
    except (ValueError, IndexError):
        return PingResult(url, now, None, None, None, None, error=f"unparseable curl output: {stdout!r}")


@dataclass
class UrlState:
    url: str
    initial_state_normal: bool = False
    last: PingResult | None = None
    longest_rtt: float = 0.0
    ever_non_200: bool = False
    error_code: str | None = None
    error_until: datetime | None = None
    error_since: datetime | None = None
    slow_rtt: float | None = None
    slow_until: datetime | None = None
    slow_since: datetime | None = None
    baseline_code: int | None = None
    baseline_error: str | None = None
    baseline_set: bool = False
    rtt_window: timedelta = timedelta(seconds=DEFAULT_RTT_WINDOW_SECONDS)
    slow_threshold_seconds: float = SLOW_THRESHOLD_SECONDS
    sticky_duration: timedelta = STICKY_DURATION
    recent_rtts: list[tuple[datetime, float]] = field(default_factory=list)

    def clear(self) -> None:
        self.last = None
        self.longest_rtt = 0.0
        self.ever_non_200 = False
        self.error_code = None
        self.error_until = None
        self.error_since = None
        self.slow_rtt = None
        self.slow_until = None
        self.slow_since = None
        self.baseline_code = None
        self.baseline_error = None
        self.baseline_set = False
        self.recent_rtts = []

    def is_bad(self, result: PingResult) -> bool:
        if not self.initial_state_normal or not self.baseline_set:
            return not result.ok
        return result.error is not None or result.http_code != self.baseline_code

    def apply(self, result: PingResult) -> bool:
        self.last = result
        if result.total_time is not None:
            self.longest_rtt = max(self.longest_rtt, result.total_time)
            self.recent_rtts.append((result.timestamp, result.total_time))
            cutoff = result.timestamp - self.rtt_window
            self.recent_rtts = [(t, r) for t, r in self.recent_rtts if t >= cutoff]

        bad = self.is_bad(result)

        if self.initial_state_normal and not self.baseline_set:
            self.baseline_code = result.http_code
            self.baseline_error = result.error
            self.baseline_set = True
            bad = False

        if bad:
            new_error_code = str(result.http_code) if result.http_code is not None else (result.error or "error")
            # A recurrence of the same error within its still-active sticky window keeps its
            # existing rank among errors; only a fresh/different error jumps to the top.
            same_ongoing_error = self.error_code == new_error_code and self.is_error(result.timestamp)
            if not same_ongoing_error:
                self.error_since = result.timestamp
            self.ever_non_200 = True
            self.error_code = new_error_code
            self.error_until = result.timestamp + self.sticky_duration
        if result.is_slow(self.slow_threshold_seconds):
            same_ongoing_warning = self.is_warning(result.timestamp)
            if not same_ongoing_warning:
                self.slow_since = result.timestamp
            self.slow_rtt = result.total_time
            self.slow_until = result.timestamp + self.sticky_duration
        return bad

    def is_error(self, now: datetime) -> bool:
        return self.error_until is not None and now < self.error_until

    def is_warning(self, now: datetime) -> bool:
        return self.slow_until is not None and now < self.slow_until

    def max_rtt_in_window(self, now: datetime) -> float | None:
        cutoff = now - self.rtt_window
        rtts = [r for t, r in self.recent_rtts if t >= cutoff]
        return max(rtts) if rtts else None


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pingwatch")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(message)s")

    for filename, level in (("info.log", logging.INFO), ("warning.log", logging.WARNING), ("error.log", logging.ERROR)):
        handler = logging.FileHandler(log_dir / filename)
        handler.setLevel(level)
        handler.addFilter(lambda record, level=level: record.levelno == level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def log_result(logger: logging.Logger, result: PingResult, is_bad: bool, slow_threshold: float) -> None:
    if result.error is not None:
        msg = f"{result.url} ERROR {result.error}"
        logger.error(msg)
        return

    msg = (
        f"{result.url} code={result.http_code} connect={result.connect_time:.3f}s "
        f"ttfb={result.ttfb:.3f}s total={result.total_time:.3f}s"
    )
    logger.info(msg)
    if is_bad:
        logger.error(msg)
    if result.is_slow(slow_threshold):
        logger.warning(msg)


def read_urls(path: Path) -> list[str]:
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def read_url_groups(path: Path) -> dict[str, list[str]]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of group name to list of URLs")
    return {str(name).upper(): [str(url) for url in (urls or [])] for name, urls in data.items()}


def load_groups(path: Path) -> dict[str, list[str]]:
    if path.suffix.lower() in (".yaml", ".yml"):
        return read_url_groups(path)
    return {"": read_urls(path)}


def fmt_seconds(value: float | None) -> str:
    return f"{value:.3f}s" if value is not None else "-"


COLUMNS = ("URL", "Last RTT", "Last HTTP code", "Longest RTT", "Baseline", "Ever Error", "HTTP Err", "Slow RTT")


class SettingsInputScreen(ModalScreen[float | None]):
    """Modal prompt used by the command palette to edit a single numeric setting."""

    def __init__(self, label: str, current_value: float) -> None:
        super().__init__()
        self.label = label
        self.current_value = current_value

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Label(self.label)
            yield Input(value=str(self.current_value), type="number")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            value = float(event.value)
        except ValueError:
            return
        self.dismiss(value)

    def key_escape(self) -> None:
        self.dismiss(None)


class SettingsCommands(Provider):
    """Command palette entries for adjusting pingwatch's live-tunable settings."""

    async def search(self, query: str) -> Hits:
        app = self.app
        assert isinstance(app, PingWatchApp)
        matcher = self.matcher(query)
        settings = (
            ("Set ping interval…", f"currently {app.interval}s", app.action_set_interval),
            ("Set slow threshold…", f"currently {app.slow_threshold_seconds}s", app.action_set_slow_threshold),
            ("Set sticky duration…", f"currently {app.sticky_duration.total_seconds()}s", app.action_set_sticky_duration),
            ("Set RTT window…", f"currently {app.rtt_window.total_seconds()}s", app.action_set_rtt_window),
            ("Clear", "reset all collected stats without losing settings", app.action_clear),
        )
        for name, help_text, callback in settings:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class PingWatchApp(App):
    BINDINGS = [("q", "quit", "Quit")]
    COMMANDS = App.COMMANDS | {SettingsCommands}
    CSS = """
    #settings-bar {
        dock: top;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        groups: dict[str, list[str]],
        interval: float,
        logger: logging.Logger,
        max_concurrent: int = 20,
        initial_state_normal: bool = False,
        rtt_window: float = DEFAULT_RTT_WINDOW_SECONDS,
        slow_threshold: float = SLOW_THRESHOLD_SECONDS,
        sticky_duration: float = STICKY_DURATION.total_seconds(),
    ):
        super().__init__()
        self.groups = groups
        self.urls = [url for urls in groups.values() for url in urls]
        self.url_group: dict[str, str] = {url: name for name, urls in groups.items() for url in urls}
        self.group_index: dict[str, int] = {name: i for i, name in enumerate(groups)}
        self.interval = interval
        self.logger = logger
        self.initial_state_normal = initial_state_normal
        self.rtt_window = timedelta(seconds=rtt_window)
        self.slow_threshold_seconds = slow_threshold
        self.sticky_duration = timedelta(seconds=sticky_duration)
        self.sem = asyncio.Semaphore(max_concurrent)
        self.states: dict[str, UrlState] = {
            url: UrlState(
                url,
                initial_state_normal=initial_state_normal,
                rtt_window=self.rtt_window,
                slow_threshold_seconds=self.slow_threshold_seconds,
                sticky_duration=self.sticky_duration,
            )
            for url in self.urls
        }
        self._row_keys: dict[str, object] = {}
        self._column_keys: dict[str, object] = {}
        self._heading_texts: dict[str, str] = {name: name for name in groups if name}
        self._heading_row_keys: dict[str, object] = {}
        self.collapsed: dict[str, bool] = {name: False for name in groups if name}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="settings-bar")
        yield DataTable()
        yield Footer()

    def update_settings_bar(self) -> None:
        self.query_one("#settings-bar", Static).update(
            "PingWatch   "
            f"interval: {self.interval}s   "
            f"slow threshold: {self.slow_threshold_seconds}s   "
            f"sticky duration: {self.sticky_duration.total_seconds()}s   "
            f"RTT window: {self.rtt_window.total_seconds()}s   "
            "(These values can be set in the palette - just search)"
        )

    def on_mount(self) -> None:
        self.update_settings_bar()
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        column_keys = table.add_columns(*COLUMNS)
        self._column_keys = dict(zip(COLUMNS, column_keys))
        for group_name, urls in self.groups.items():
            if group_name:
                heading = Text("\n")
                heading.append(group_name, style="bold reverse")
                heading_key = table.add_row(
                    heading,
                    *(Text("\n"),) * (len(COLUMNS) - 1),
                    height=2,
                    key=f"__group__{group_name}",
                )
                self._heading_row_keys[group_name] = heading_key
            for url in urls:
                key = table.add_row(*("-",) * len(COLUMNS), key=url)
                self._row_keys[url] = key
                self.render_row(url)

        for group_name in self._heading_row_keys:
            self.update_group_heading(group_name)

        for url in self.urls:
            self.run_worker(self.ping_loop(url), exclusive=False)

        self.set_interval(1.0, self.refresh_row_colors)

    async def ping_loop(self, url: str) -> None:
        state = self.states[url]
        while True:
            result = await ping(url, self.sem)
            is_bad = state.apply(result)
            log_result(self.logger, result, is_bad, state.slow_threshold_seconds)
            self.render_row(url)
            self.reorder_rows()
            await asyncio.sleep(self.interval)

    def row_style(self, url: str) -> str:
        state = self.states[url]
        now = datetime.now()
        if state.is_error(now):
            return "white on red"
        if state.is_warning(now):
            return "black on yellow"
        return ""

    def render_row(self, url: str) -> None:
        if url not in self._row_keys:
            return
        state = self.states[url]
        result = state.last
        now = datetime.now()
        table = self.query_one(DataTable)
        row_key = self._row_keys[url]

        last_rtt = fmt_seconds(result.total_time) if result else "-"
        last_response = f"HTTP {result.http_code}" if result and result.http_code else (result.error if result else "-")
        longest_rtt = fmt_seconds(state.longest_rtt) if state.longest_rtt else "-"
        if not state.initial_state_normal:
            baseline = "-"
        elif not state.baseline_set:
            baseline = "-"
        elif state.baseline_code is not None:
            baseline = f"HTTP {state.baseline_code}"
        else:
            baseline = state.baseline_error or "error"
        ever_non_200 = "true" if state.ever_non_200 else "false"
        http_err = state.error_code if state.is_error(now) else "-"
        slow_rtt = fmt_seconds(state.slow_rtt) if state.is_warning(now) else "-"

        style = self.row_style(url)
        values = (url, last_rtt, last_response, longest_rtt, baseline, ever_non_200, http_err, slow_rtt)
        for col, value in zip(COLUMNS, values):
            table.update_cell(row_key, self._column_keys[col], Text(value, style=style))

    def refresh_row_colors(self) -> None:
        for url in self.urls:
            self.render_row(url)
        for group_name in self._heading_row_keys:
            self.update_group_heading(group_name)
        self.reorder_rows()

    def action_set_interval(self) -> None:
        self.push_screen(
            SettingsInputScreen("Ping interval (seconds)", self.interval),
            self._apply_interval,
        )

    def _apply_interval(self, value: float | None) -> None:
        if value is None:
            return
        self.interval = value
        self.update_settings_bar()

    def action_set_slow_threshold(self) -> None:
        self.push_screen(
            SettingsInputScreen("Slow threshold (seconds)", self.slow_threshold_seconds),
            self._apply_slow_threshold,
        )

    def _apply_slow_threshold(self, value: float | None) -> None:
        if value is None:
            return
        self.slow_threshold_seconds = value
        for state in self.states.values():
            state.slow_threshold_seconds = value
        self.refresh_row_colors()
        self.update_settings_bar()

    def action_set_sticky_duration(self) -> None:
        self.push_screen(
            SettingsInputScreen("Sticky duration (seconds)", self.sticky_duration.total_seconds()),
            self._apply_sticky_duration,
        )

    def _apply_sticky_duration(self, value: float | None) -> None:
        if value is None:
            return
        self.sticky_duration = timedelta(seconds=value)
        for state in self.states.values():
            state.sticky_duration = self.sticky_duration
        self.refresh_row_colors()
        self.update_settings_bar()

    def action_set_rtt_window(self) -> None:
        self.push_screen(
            SettingsInputScreen("RTT window (seconds)", self.rtt_window.total_seconds()),
            self._apply_rtt_window,
        )

    def _apply_rtt_window(self, value: float | None) -> None:
        if value is None:
            return
        self.rtt_window = timedelta(seconds=value)
        for state in self.states.values():
            state.rtt_window = self.rtt_window
        self.refresh_row_colors()
        self.update_settings_bar()

    def action_clear(self) -> None:
        for state in self.states.values():
            state.clear()
        self.refresh_row_colors()

    def update_group_heading(self, group_name: str) -> None:
        table = self.query_one(DataTable)
        row_key = self._heading_row_keys[group_name]
        urls = self.groups[group_name]
        now = datetime.now()
        error_count = sum(1 for u in urls if self.states[u].is_error(now))
        warning_count = sum(1 for u in urls if self.states[u].is_warning(now))
        rtts = [
            rtt for u in urls
            if (rtt := self.states[u].max_rtt_in_window(now)) is not None
        ]
        max_rtt = fmt_seconds(max(rtts)) if rtts else "-"
        arrow = "▶" if self.collapsed[group_name] else "▼"

        def cell(text: str) -> Text:
            value = Text("\n")
            value.append(text, style="bold reverse")
            return value

        table.update_cell(row_key, self._column_keys["Last RTT"], cell(arrow))
        table.update_cell(row_key, self._column_keys["Longest RTT"], cell(f"RTT {max_rtt}"))
        table.update_cell(row_key, self._column_keys["HTTP Err"], cell(f"{error_count} errors"))
        table.update_cell(row_key, self._column_keys["Slow RTT"], cell(f"{warning_count} warnings"))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if isinstance(key, str) and key.startswith("__group__"):
            self.toggle_group(key[len("__group__"):])

    def toggle_group(self, group_name: str) -> None:
        self.collapsed[group_name] = not self.collapsed[group_name]
        table = self.query_one(DataTable)
        if self.collapsed[group_name]:
            for url in self.groups[group_name]:
                if url in self._row_keys:
                    table.remove_row(self._row_keys.pop(url))
        else:
            for url in self.groups[group_name]:
                key = table.add_row(*("-",) * len(COLUMNS), key=url)
                self._row_keys[url] = key
                self.render_row(url)
        self.update_group_heading(group_name)
        self.reorder_rows()

    def row_sort_key(self, url_cell: Text | str) -> tuple:
        text = (url_cell.plain if isinstance(url_cell, Text) else str(url_cell)).strip()
        group_name = self._heading_texts.get(text)
        if group_name is not None:
            return (self.group_index[group_name], -1, "")
        state = self.states[text]
        group_idx = self.group_index[self.url_group[text]]
        now = datetime.now()
        if state.is_error(now):
            return (group_idx, 0, -state.error_since.timestamp())
        if state.is_warning(now):
            return (group_idx, 1, -state.slow_since.timestamp())
        return (group_idx, 2, text)

    def reorder_rows(self) -> None:
        table = self.query_one(DataTable)
        table.sort(self._column_keys["URL"], key=self.row_sort_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "urls_file",
        type=Path,
        help="URL list file: a .yaml/.yml file of group name -> list of URLs, or a plain-text file with one URL per line",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between pings per URL (default: 1.0)")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="Directory for info/warning/error logs")
    parser.add_argument("--max-concurrent", type=int, default=20, help="Max curl processes in flight at once (default: 20)")
    parser.add_argument(
        "--initial-state-normal",
        action="store_true",
        help="Treat each URL's first observed response as its baseline; flag any deviation (not just non-200) as an error",
    )
    parser.add_argument(
        "--rtt-window",
        type=float,
        default=DEFAULT_RTT_WINDOW_SECONDS,
        help=f"Rolling window in seconds for each group's 'highest RTT' heading figure (default: {DEFAULT_RTT_WINDOW_SECONDS})",
    )
    parser.add_argument(
        "--slow-threshold",
        type=float,
        default=SLOW_THRESHOLD_SECONDS,
        help=f"Total request time in seconds above which a result is flagged as slow (default: {SLOW_THRESHOLD_SECONDS})",
    )
    parser.add_argument(
        "--sticky-duration",
        type=float,
        default=STICKY_DURATION.total_seconds(),
        help=f"Seconds an error/slow highlight stays visible after it clears (default: {STICKY_DURATION.total_seconds()})",
    )
    args = parser.parse_args()

    groups = load_groups(args.urls_file)
    urls = [url for urls in groups.values() for url in urls]
    if not urls:
        parser.error(f"No URLs found in {args.urls_file}")

    logger = setup_logging(args.log_dir)
    app = PingWatchApp(
        groups,
        args.interval,
        logger,
        max_concurrent=args.max_concurrent,
        initial_state_normal=args.initial_state_normal,
        rtt_window=args.rtt_window,
        slow_threshold=args.slow_threshold,
        sticky_duration=args.sticky_duration,
    )
    app.run()


if __name__ == "__main__":
    main()
