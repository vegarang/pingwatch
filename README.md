# pingwatch

A terminal UI for watching the health of a list of URLs. Pings each URL concurrently on a fixed interval using `curl`, and shows live status in a `textual`-based table — with rows highlighted red on non-200 responses/errors and yellow on slow responses. Rows are kept sorted so errors float to the top, warnings sit just below current errors, and normal URLs are sorted alphabetically below both.

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
./pingwatch.py urls.txt
```

The URL file has one URL per line; blank lines and lines starting with `#` are ignored. `urls.txt` is a sample file exercising healthy, erroring, redirecting, slow, and unreachable URLs. `urls/` is gitignored — use it to keep your own (e.g. project-specific) URL lists locally without committing them.

### Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | `1.0` | Seconds between pings per URL |
| `--log-dir` | `logs` | Directory for `info.log`, `warning.log`, `error.log` |
| `--max-concurrent` | `20` | Max concurrent `curl` processes |
| `--initial-state-normal` | off | Treat each URL's first observed response as its baseline; flag deviations instead of non-200 |

Press `q` to quit.

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
- **HTTP Err** — sticky (5 min) error code/message after a failure
- **Slow RTT** — sticky (5 min) RTT after a response slower than 3s

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## curlping

`curlping <url>` is a small standalone bash script that curls a single URL once per second and prints connect/TTFB/total time and response code, without the TUI.
