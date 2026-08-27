# pingwatch

A terminal UI for watching the health of a list of URLs. Pings each URL concurrently on a fixed interval using `curl`, and shows live status in a `textual`-based table — with rows highlighted red on non-200 responses/errors and yellow on slow responses. URLs can be organized into named groups, each shown as its own labeled section; within a group, rows are kept sorted so errors float to the top, warnings sit just below current errors, and normal URLs are sorted alphabetically below both.

## Requirements

- Python 3
- `curl` on your `PATH`
- [`textual`](https://textual.textualize.io/) (see `requirements.txt`)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
./pingwatch.py urls.yaml
```

The URL file can be either:

- A **YAML file** (`.yaml`/`.yml`) mapping group name to a list of URLs. Each group is rendered as its own labeled section in the table:
  ```yaml
  my-group:
    - url1.example.com
    - url2.example.com
  other-group:
    - other.url.com
    - other2.url.com
  ```
- A **plain-text file** (any other extension) with one URL per line; blank lines and lines starting with `#` are ignored. All URLs are treated as one ungrouped list, with no heading shown.

`urls.txt` is a sample plain-text file exercising healthy, erroring, redirecting, slow, and unreachable URLs. `urls/` is gitignored — use it to keep your own (e.g. project-specific) URL lists locally without committing them.

### Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | `1.0` | Seconds between pings per URL |
| `--log-dir` | `logs` | Directory for `info.log`, `warning.log`, `error.log` |
| `--max-concurrent` | `20` | Max concurrent `curl` processes |
| `--initial-state-normal` | off | Treat each URL's first observed response as its baseline; flag deviations instead of non-200 |
| `--rtt-window` | `60` | Rolling window in seconds for each group's "highest RTT" heading figure |
| `--slow-threshold` | `3` | Total request time in seconds above which a result is flagged as slow |
| `--sticky-duration` | `300` | Seconds an error/slow highlight stays visible after it clears |

Press `q` to quit.

### Changing settings at runtime

`--interval`, `--slow-threshold`, `--sticky-duration`, and `--rtt-window` can also be changed while pingwatch is running, without restarting it, via the command palette (`ctrl+p`, or click the "☰ Palette" item in the footer). Search for one of:

- **Set ping interval…**
- **Set slow threshold…**
- **Set sticky duration…**
- **Set RTT window…**

Each opens a small dialog pre-filled with the current value (in seconds) — enter a new number and press enter to apply, or `esc` to cancel. The change applies immediately to every URL and updates row highlighting right away; it's not persisted, so it reverts to the command-line value (or default) on the next run.

The palette also has a **Clear** command, which resets every URL's collected stats — Last HTTP code, Longest RTT, Baseline, Ever Non-200, HTTP Err, and Slow RTT — back to their initial state, without touching the current settings (interval, slow threshold, sticky duration, RTT window).

### Baseline mode (`--initial-state-normal`)

Some URLs are expected to return a non-200 status (redirects, auth-gated 401/403s, deprecated 410s, etc). Passing `--initial-state-normal` switches the whole run from "200 is ok" to "whatever the URL returned on its first ping is ok" — every URL's baseline is captured independently from its own first response. After that, a URL is only flagged as an error when its status code (or a curl-level failure) *changes* from that baseline, which is useful for catching regressions in surrounding infrastructure (proxies, gateways, etc.) without the noise of URLs that are non-200 by design.

This is a global, whole-run switch — it applies to every URL in the file. If you have a mix of URLs that should always be 200 and URLs with an expected non-200 baseline, put them in separate URL list files and run pingwatch twice. The baseline is only kept in memory for the current run; restarting pingwatch re-captures it from scratch. The **Baseline** column shows what was recorded for each URL.

### Table columns

- **URL**
- **Last RTT** — total time of the most recent request
- **Last HTTP code** — status code or error from the most recent request
- **Longest RTT** — slowest total time seen so far
- **Baseline** — the status code (or error) recorded on the first ping when `--initial-state-normal` is set; `-` otherwise
- **Ever Non-200** — whether any request has returned a non-200 status or errored
- **HTTP Err** — sticky (`--sticky-duration`, default 5 min) error code/message after a failure
- **Slow RTT** — sticky (`--sticky-duration`, default 5 min) RTT after a response slower than `--slow-threshold` (default 3s)

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## curlping

`curlping <url>` is a small standalone bash script that curls a single URL once per second and prints connect/TTFB/total time and response code, without the TUI.
