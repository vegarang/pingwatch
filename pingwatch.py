#!/usr/bin/env python3
"""Ping a list of URLs concurrently and show live status in a terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

SLOW_THRESHOLD_SECONDS = 3
STICKY_DURATION = timedelta(minutes=5)


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
        return self.total_time is not None and self.total_time > SLOW_THRESHOLD_SECONDS


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

    def is_bad(self, result: PingResult) -> bool:
        if not self.initial_state_normal or not self.baseline_set:
            return not result.ok
        return result.error is not None or result.http_code != self.baseline_code

    def apply(self, result: PingResult) -> bool:
        self.last = result
        if result.total_time is not None:
            self.longest_rtt = max(self.longest_rtt, result.total_time)

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
            self.error_until = result.timestamp + STICKY_DURATION
        if result.slow:
            same_ongoing_warning = self.is_warning(result.timestamp)
            if not same_ongoing_warning:
                self.slow_since = result.timestamp
            self.slow_rtt = result.total_time
            self.slow_until = result.timestamp + STICKY_DURATION
        return bad

    def is_error(self, now: datetime) -> bool:
        return self.error_until is not None and now < self.error_until

    def is_warning(self, now: datetime) -> bool:
        return self.slow_until is not None and now < self.slow_until


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


def log_result(logger: logging.Logger, result: PingResult, is_bad: bool) -> None:
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
    if result.slow:
        logger.warning(msg)


def read_urls(path: Path) -> list[str]:
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def fmt_seconds(value: float | None) -> str:
    return f"{value:.3f}s" if value is not None else "-"


COLUMNS = ("URL", "Last RTT", "Last HTTP code", "Longest RTT", "Baseline", "Ever Non-200", "HTTP Err", "Slow RTT")


class PingWatchApp(App):
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(
        self,
        urls: list[str],
        interval: float,
        logger: logging.Logger,
        max_concurrent: int = 20,
        initial_state_normal: bool = False,
    ):
        super().__init__()
        self.urls = urls
        self.interval = interval
        self.logger = logger
        self.initial_state_normal = initial_state_normal
        self.sem = asyncio.Semaphore(max_concurrent)
        self.states: dict[str, UrlState] = {
            url: UrlState(url, initial_state_normal=initial_state_normal) for url in urls
        }
        self._row_keys: dict[str, object] = {}
        self._column_keys: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable()
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        column_keys = table.add_columns(*COLUMNS)
        self._column_keys = dict(zip(COLUMNS, column_keys))
        for url in self.urls:
            key = table.add_row(*("-",) * len(COLUMNS), key=url)
            self._row_keys[url] = key
            self.render_row(url)

        for url in self.urls:
            self.run_worker(self.ping_loop(url), exclusive=False)

        self.set_interval(1.0, self.refresh_row_colors)

    async def ping_loop(self, url: str) -> None:
        state = self.states[url]
        while True:
            result = await ping(url, self.sem)
            is_bad = state.apply(result)
            log_result(self.logger, result, is_bad)
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
        self.reorder_rows()

    def row_sort_key(self, url_cell: Text | str) -> tuple:
        url = url_cell.plain if isinstance(url_cell, Text) else str(url_cell)
        state = self.states[url]
        now = datetime.now()
        if state.is_error(now):
            return (0, -state.error_since.timestamp())
        if state.is_warning(now):
            return (1, -state.slow_since.timestamp())
        return (2, url)

    def reorder_rows(self) -> None:
        table = self.query_one(DataTable)
        table.sort(self._column_keys["URL"], key=self.row_sort_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls_file", type=Path, help="File with one URL per line")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between pings per URL (default: 1.0)")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="Directory for info/warning/error logs")
    parser.add_argument("--max-concurrent", type=int, default=20, help="Max curl processes in flight at once (default: 20)")
    parser.add_argument(
        "--initial-state-normal",
        action="store_true",
        help="Treat each URL's first observed response as its baseline; flag any deviation (not just non-200) as an error",
    )
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    if not urls:
        parser.error(f"No URLs found in {args.urls_file}")

    logger = setup_logging(args.log_dir)
    app = PingWatchApp(
        urls,
        args.interval,
        logger,
        max_concurrent=args.max_concurrent,
        initial_state_normal=args.initial_state_normal,
    )
    app.run()


if __name__ == "__main__":
    main()
