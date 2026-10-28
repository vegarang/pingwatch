# pingwatch

A terminal UI for watching the health of a list of URLs. Pings each URL concurrently on a fixed interval using `aiohttp`, and shows live status in a `textual`-based table — with rows highlighted red on non-200 responses/errors and yellow on slow responses. URLs can be organized into named groups, each shown as its own labeled section; within a group, rows are kept sorted so errors float to the top, warnings sit just below current errors, and normal URLs are sorted alphabetically below both.

## Requirements

- Python 3
- `aiohttp`, `pyyaml`, and [`textual`](https://textual.textualize.io/) (see `requirements.txt`)

## Setup

```bash
uv venv --python 3.13 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

Using [`uv`](https://docs.astral.sh/uv/) here (instead of a plain `python3 -m venv`) matters: it manages its own Python builds independent of Homebrew, so `.venv` keeps working across `brew upgrade` runs that remove a versioned `python@X.Y` formula. If `.venv` ever breaks after a Homebrew upgrade (e.g. `ModuleNotFoundError` for a package that's clearly installed), it's because the venv was pinned to a Homebrew Python that Homebrew has since deleted — the fix is to rebuild it:

```bash
rm -rf .venv
uv venv --python 3.13 .venv
uv pip install -r requirements.txt -r requirements-dev.txt
```

## Usage

```bash
./pingwatch.py urls.yaml
```

The URL file can be either:

- A **YAML file** (`.yaml`/`.yml`) mapping group name to a list of URLs. Each group is rendered as its own labeled section in the table:
  ```yaml
  my-group:
    - https://url1.example.com
    - https://url2.example.com
  other-group:
    - https://other.url.com
    - https://other2.url.com
  ```
- A **plain-text file** (any other extension) with one URL per line; blank lines and lines starting with `#` are ignored. All URLs are treated as one ungrouped list, with no heading shown.

`urls.txt` is a sample plain-text file exercising healthy, erroring, redirecting, slow, and unreachable URLs. `urls/` is gitignored — use it to keep your own (e.g. project-specific) URL lists locally without committing them.

### Optional connection destination (`connect_to`)

YAML groups can mix URL strings with entries containing `url`, an optional `name`, and an optional `connect_to` destination:

```yaml
trailer:
  - name: normal
    url: https://api.example.com/api
  - name: front-door
    url: https://api.example.com/api
    connect_to:
      host: edge.example.net
      port: 443
```

The `front-door` entry connects to the edge hostname on port 443, with the connection behavior of:

```bash
curl -v -o /dev/null \
  --connect-to 'api.example.com:443:edge.example.net:443' \
  'https://api.example.com/api'
```

The request URL, HTTP `Host` header, TLS SNI, and certificate verification continue to use `api.example.com`. Only the network destination changes. Pingwatch consumes the response body without displaying it and reports timings and errors in its table and logs; it does not produce curl's verbose trace.

`connect_to.host` is required when using an override and accepts a hostname, IPv4 address, or unbracketed IPv6 address. `connect_to.port` is optional and defaults to the URL's effective port (443 for HTTPS, 80 for HTTP, or the URL's explicit port); an explicit destination port must be an integer from 1 to 65535. Structured entries require absolute HTTP(S) URLs. Existing URL strings and plain-text files still work.

Every entry has independent statistics, baselines, and highlights, including repeated URLs within or across groups. Names appear alongside URLs; the **Connect to** column shows the override destination, and logs include the entry ID, name when supplied, and destination when overridden. Connections are pooled separately for each source/destination route, with `--max-concurrent` limiting all requests together.

Redirects retain pingwatch's existing follow behavior. An override applies only to hops matching the entry's original host and port; other destinations connect normally. Destination DNS, connection, timeout, and TLS failures are reported as errors without falling back to the original destination. Wildcard rules and arbitrary curl options are not supported.

### Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | `1.0` | Seconds between pings per URL |
| `--log-dir` | `logs` | Directory for `info.log`, `warning.log`, `error.log` |
| `--max-concurrent` | `20` | Max concurrent HTTP requests across all entries |
| `--requests-per-url` | `1` | Independent, overlapping ping streams to run per URL |
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

Some URLs are expected to return a non-200 status (redirects, auth-gated 401/403s, deprecated 410s, etc). Passing `--initial-state-normal` switches the whole run from "200 is ok" to "whatever the URL returned on its first ping is ok" — every entry's baseline is captured independently from its own first response. After that, a URL is flagged as an error when its status code differs from that baseline or an HTTP client failure occurs, which is useful for catching regressions in surrounding infrastructure (proxies, gateways, etc.) without the noise of URLs that are non-200 by design.

This is a global, whole-run switch — it applies to every URL in the file. If you have a mix of URLs that should always be 200 and URLs with an expected non-200 baseline, put them in separate URL list files and run pingwatch twice. The baseline is only kept in memory for the current run; restarting pingwatch re-captures it from scratch. The **Baseline** column shows what was recorded for each URL.

### Table columns

- **URL** — original URL, prefixed with the entry's name when supplied
- **Connect to** — override destination host and port, or `-` for normal DNS
- **Last RTT** — total time of the most recent request
- **Last HTTP code** — status code or error from the most recent request
- **Longest RTT** — slowest total time seen so far
- **Baseline** — the status code (or error) recorded on the first ping when `--initial-state-normal` is set; `-` otherwise
- **Ever Non-200** — whether any request has returned a non-200 status or errored
- **HTTP Err** — sticky (`--sticky-duration`, default 5 min) error code/message after a failure
- **Slow RTT** — sticky (`--sticky-duration`, default 5 min) RTT after a response slower than `--slow-threshold` (default 3s)

## Development

```bash
uv pip install -r requirements-dev.txt
pytest
```

The HTTP/TLS tests start local loopback servers and use `trustme` to generate test certificates. Connection overrides use aiohttp's internal `_resolve_host` connector hook; run the full suite when upgrading aiohttp to verify destination routing, SNI, and certificate verification.

## curlping

`curlping <url>` is a small standalone bash script that curls a single URL once per second and prints connect/TTFB/total time and response code, without the TUI. It requires `curl` on your `PATH`.
