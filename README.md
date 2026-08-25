# pingwatch

A terminal UI for watching the health of a list of URLs. Pings each URL concurrently on a fixed interval using `curl`, and shows live status in a `textual`-based table — with rows highlighted red on non-200 responses/errors and yellow on slow responses.

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

Press `q` to quit.

### Table columns

- **URL**
- **Last RTT** — total time of the most recent request
- **Last HTTP code** — status code or error from the most recent request
- **Longest RTT** — slowest total time seen so far
- **Ever Non-200** — whether any request has returned a non-200 status or errored
- **HTTP Err** — sticky (5 min) error code/message after a failure
- **Slow RTT** — sticky (5 min) RTT after a response slower than 3s

## curlping

`curlping <url>` is a small standalone bash script that curls a single URL once per second and prints connect/TTFB/total time and response code, without the TUI.
