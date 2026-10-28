"""Configuration, routing, TLS, and UI coverage for connection overrides."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import sys
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
import trustme
import yaml
from textual.widgets import DataTable

import pingwatch
from pingwatch import ConnectTo, PingWatchApp, Target, load_groups, make_session, ping
from test_pingwatch import make_result


@asynccontextmanager
async def serve(handler, ssl_context=None):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(ssl=ssl_context)
    try:
        yield server
    finally:
        await server.close()


def write_config(tmp_path, entries):
    path = tmp_path / "urls.yaml"
    path.write_text(yaml.safe_dump({"trailer": entries}))
    return path


def test_config_mixes_strings_named_targets_and_overrides(tmp_path):
    url = "https://api.example.com/api"
    path = write_config(tmp_path, [
        url,
        {"url": url, "name": "normal"},
        {"url": url, "name": "front-door", "connect_to": {
            "host": "edge.example.net", "port": 443,
        }},
    ])
    normal, named, overridden = load_groups(path)["TRAILER"]
    assert len({normal.id, named.id, overridden.id}) == 3
    assert normal.url == named.url == overridden.url == url
    assert normal.connect_to is named.connect_to is None
    assert overridden.connect_to == ConnectTo("edge.example.net", 443)
    assert overridden.route.source_host == "api.example.com"
    assert overridden.route.source_port == 443


@pytest.mark.parametrize("url,port", [
    ("https://origin.test/api", 443),
    ("http://origin.test/api", 80),
    ("https://origin.test:8443/api", 8443),
])
def test_destination_port_defaults_to_url_port(tmp_path, url, port):
    path = write_config(tmp_path, [{"url": url, "connect_to": {"host": "EDGE.test"}}])
    target = load_groups(path)["TRAILER"][0]
    assert target.connect_to == ConnectTo("edge.test", port)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_config_accepts_ip_destinations(tmp_path, host):
    path = write_config(tmp_path, [{"url": "http://origin.test", "connect_to": {"host": host}}])
    assert load_groups(path)["TRAILER"][0].connect_to == ConnectTo(host, 80)


@pytest.mark.parametrize("entry", [
    123,
    None,
    {},
    {"url": "origin.test"},
    {"url": "ftp://origin.test/api"},
    {"url": "https://"},
    {"url": "https://origin.test:0"},
    {"url": "https://origin.test:65536"},
    {"url": "https://origin.test:notaport"},
    {"url": "https://bad host.test"},
    {"url": 123},
    {"url": "http://origin.test", "name": False},
    {"url": "http://origin.test", "name": ""},
    {"url": "http://origin.test", "connect-to": {}},
    {"url": "http://origin.test", "connect_to": None},
    {"url": "http://origin.test", "connect_to": "edge.test:80"},
    {"url": "http://origin.test", "connect_to": {}},
    {"url": "http://origin.test", "connect_to": {"host": 123}},
    {"url": "http://origin.test", "connect_to": {"host": ""}},
    {"url": "http://origin.test", "connect_to": {"host": "https://edge.test"}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test:443"}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test/path"}},
    {"url": "http://origin.test", "connect_to": {"host": "edge test"}},
    {"url": "http://origin.test", "connect_to": {"host": "[::1]"}},
    {"url": "http://origin.test", "connect_to": {"host": "127.1"}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "port": True}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "port": "443"}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "port": 0}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "port": 65536}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "port": 1.5}},
    {"url": "http://origin.test", "connect_to": {"host": "edge.test", "typo": 443}},
])
def test_invalid_entries_report_file_group_and_position(tmp_path, entry):
    path = write_config(tmp_path, ["http://good.test", entry])
    with pytest.raises(ValueError, match="group 'TRAILER', entry 2") as exc:
        load_groups(path)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize("text", ["[]", "false", "group: http://origin.test", "group: []\nGROUP: []"])
def test_invalid_groups_rejected(tmp_path, text):
    path = tmp_path / "urls.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_groups(path)


@pytest.mark.parametrize("text", ["", "group:", "group: []"])
def test_empty_groups_supported(tmp_path, text):
    path = tmp_path / "urls.yaml"
    path.write_text(text)
    assert not any(load_groups(path).values())


def test_invalid_config_exits_before_logging_or_ui(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, [{"url": "invalid"}])
    monkeypatch.setattr(sys, "argv", ["pingwatch.py", str(path)])

    def unexpected_logging(*args):
        pytest.fail("logging must not start before configuration validation")

    monkeypatch.setattr(pingwatch, "setup_logging", unexpected_logging)
    with pytest.raises(SystemExit) as exc:
        pingwatch.main()
    assert exc.value.code == 2
    assert "group 'TRAILER', entry 1" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("source_host", ["origin.invalid", "192.0.2.1", "[2001:db8::1]"])
async def test_http_override_preserves_host_path_query_and_reuses_connection(source_host):
    requests = []

    async def handler(request):
        requests.append((request.host, request.raw_path, request.transport))
        return web.Response(text="healthy")

    async with serve(handler) as server:
        url = f"http://{source_host}:54321/api?check=1&value=two"
        target = Target("test", url, connect_to=ConnectTo("127.0.0.1", server.port))
        async with make_session(2, target.route) as session:
            for _ in range(2):
                result = await ping(url, session, asyncio.Semaphore(2))
                assert result.ok, result.error
                assert result.url == url
                assert result.connect_time is not None
                assert result.ttfb is not None
                assert result.total_time is not None
    assert [(host, path) for host, path, _ in requests] == [
        (f"{source_host}:54321", "/api?check=1&value=two"),
    ] * 2
    assert requests[0][2] is requests[1][2]


@pytest.mark.asyncio
async def test_destination_hostname_is_resolved_instead_of_original(monkeypatch):
    async def handler(request):
        return web.Response()

    async with serve(handler) as server:
        target = Target("test", "http://origin.invalid/api", connect_to=ConnectTo("edge.test", server.port))
        async with make_session(1, target.route) as session:
            lookups = []

            async def resolve(host, port, family):
                lookups.append((host, port))
                return [{"hostname": host, "host": "127.0.0.1", "port": port,
                         "family": socket.AF_INET, "proto": 0, "flags": 0}]

            monkeypatch.setattr(session.connector._resolver, "resolve", resolve)
            result = await ping(target.url, session, asyncio.Semaphore(1))
            assert result.ok, result.error
            assert lookups == [("edge.test", server.port)]


@pytest.mark.asyncio
@pytest.mark.parametrize("certificate_host, succeeds", [("origin.test", True), ("localhost", False)])
async def test_https_uses_original_sni_and_certificate_hostname(certificate_host, succeeds, monkeypatch):
    ca = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert(certificate_host).configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    names, hosts = [], []
    server_context.set_servername_callback(lambda sock, name, ctx: names.append(name))

    async def handler(request):
        hosts.append(request.host)
        return web.Response()

    async with serve(handler, server_context) as server:
        target = Target("test", "https://origin.test/api", connect_to=ConnectTo("localhost", server.port))
        async with make_session(1, target.route) as session:
            original_get = session.get

            def trusted_get(*args, **kwargs):
                return original_get(*args, ssl=client_context, **kwargs)

            monkeypatch.setattr(session, "get", trusted_get)
            result = await ping(target.url, session, asyncio.Semaphore(1))
            if succeeds:
                assert result.ok, result.error
                assert hosts == ["origin.test"]
            else:
                assert result.http_code is None
                assert "certificate" in result.error.lower()
                assert "origin.test" in result.error
                assert hosts == []
    assert names and set(names) == {"origin.test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_kind", ["same-origin", "different-host", "different-port"])
async def test_redirect_overrides_only_matching_origin(redirect_kind):
    visited = []

    async def final_handler(request):
        visited.append(("final", request.host))
        return web.Response(status=201)

    async with serve(final_handler) as final_server:
        async def initial_handler(request):
            visited.append(("initial", request.host))
            if request.path == "/done":
                return web.Response(status=202)
            if redirect_kind == "same-origin":
                location = "/done"
            elif redirect_kind == "different-host":
                location = str(final_server.make_url("/done"))
            else:
                location = f"http://localhost:{final_server.port}/done"
            raise web.HTTPFound(location)

        async with serve(initial_handler) as initial_server:
            source_port = 80 if redirect_kind == "different-port" else final_server.port
            target = Target("test", f"http://localhost:{source_port}/start",
                            connect_to=ConnectTo("127.0.0.1", initial_server.port))
            async with make_session(1, target.route) as session:
                result = await ping(target.url, session, asyncio.Semaphore(1))
            assert result.error is None
            assert result.http_code == (202 if redirect_kind == "same-origin" else 201)
    assert [name for name, _ in visited] == ["initial", "initial" if redirect_kind == "same-origin" else "final"]


@pytest.mark.asyncio
async def test_destination_dns_failure_does_not_fall_back(monkeypatch):
    target = Target("test", "http://origin.test/", connect_to=ConnectTo("edge.invalid", 8080))
    async with make_session(1, target.route) as session:
        lookups = []

        async def resolve(host, port, family):
            lookups.append((host, port))
            raise socket.gaierror(socket.EAI_NONAME, "destination not found")

        monkeypatch.setattr(session.connector._resolver, "resolve", resolve)
        result = await ping(target.url, session, asyncio.Semaphore(1))
        assert result.http_code is None
        assert "destination not found" in result.error
        assert lookups == [("edge.invalid", 8080)]


@pytest.mark.asyncio
async def test_connection_failure_and_timeout_are_results(monkeypatch):
    async def handler(request):
        await asyncio.sleep(0.1)
        return web.Response()

    async with serve(handler) as server:
        target = Target("test", "http://origin.invalid/", connect_to=ConnectTo("127.0.0.1", server.port))
        monkeypatch.setattr(pingwatch, "REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.01))
        async with make_session(1, target.route) as session:
            result = await ping(target.url, session, asyncio.Semaphore(1))
            assert result.error == "request timed out"
    # The server's listening socket has closed, so this route cannot connect.
    async with make_session(1, target.route) as session:
        result = await ping(target.url, session, asyncio.Semaphore(1))
        assert result.http_code is None
        assert result.error


@pytest.mark.asyncio
async def test_normal_and_overridden_variants_stay_independent_and_close_sessions(tmp_path):
    seen = []
    active = peak_active = 0

    async def respond(request, name, status):
        nonlocal active, peak_active
        active += 1
        peak_active = max(active, peak_active)
        try:
            seen.append((name, request.transport))
            await asyncio.sleep(0.005)
            return web.Response(status=status)
        finally:
            active -= 1

    async def normal_handler(request):
        return await respond(request, "normal", 200)

    async def edge_handler(request):
        return await respond(request, "edge", 201)

    async with serve(normal_handler) as normal, serve(edge_handler) as edge:
        url = str(normal.make_url("/api"))
        path = write_config(tmp_path, [url, {"url": url, "connect_to": {"host": "127.0.0.1", "port": edge.port}}])
        app = PingWatchApp(load_groups(path), interval=0.01, logger=logging.getLogger("routes"),
                           initial_state_normal=True, requests_per_url=2, max_concurrent=1)
        async with app.run_test() as pilot:
            for _ in range(50):
                if len(seen) >= 8 and all(state.last is not None for state in app.states.values()):
                    break
                await pilot.pause(0.01)
            first, second = app.groups["TRAILER"]
            assert len(app.sessions) == 2
            assert app.states[first.id].baseline_code == 200
            assert app.states[second.id].baseline_code == 201
            assert app.states[first.id].last.http_code == 200
            assert app.states[second.id].last.http_code == 201
            normal_connections = {transport for name, transport in seen if name == "normal"}
            edge_connections = {transport for name, transport in seen if name == "edge"}
            assert normal_connections and edge_connections
            assert normal_connections.isdisjoint(edge_connections)
            assert len(normal_connections) == len(edge_connections) == 1
            assert peak_active == 1
        assert all(session.closed for session in app.sessions.values())


@pytest.mark.asyncio
async def test_duplicate_rows_sort_collapse_clear_and_share_only_matching_routes(monkeypatch):
    ready = asyncio.Event()

    async def blocked_ping(url, session, sem):
        await ready.wait()

    monkeypatch.setattr(pingwatch, "ping", blocked_ping)
    url = "https://origin.test/api"
    destination = ConnectTo("edge.test", 443)
    app = PingWatchApp({
        # A group heading can equal a URL's display text without affecting identity.
        url: [Target("", url), Target("", url, name="edge", connect_to=destination),
              Target("", url, connect_to=destination)],
        "other": [Target("", url)],
    }, interval=1, logger=logging.getLogger("duplicates"), initial_state_normal=True)
    async with app.run_test():
        table = app.query_one(DataTable)
        assert len(app.states) == 4
        assert table.row_count == 6
        assert len(app.sessions) == 2
        first, edge, repeated_edge = app.groups[url]
        assert edge.route == repeated_edge.route
        app.states[first.id].apply(make_result(url=url, http_code=200))
        app.states[edge.id].apply(make_result(url=url, http_code=201))
        app.states[edge.id].apply(make_result(url=url, http_code=500))
        app.refresh_row_colors()
        row_ids = [row.key.value for row in table.ordered_rows]
        assert row_ids[1] == edge.id
        assert app.states[first.id].error_code is None
        assert app.states[edge.id].error_code == "500"
        assert table.get_cell(edge.id, app._column_keys["Connect to"]).plain == "edge.test:443"
        assert table.get_cell(edge.id, app._column_keys["URL"]).plain == f"edge: {url}"
        app.toggle_group(url)
        assert table.row_count == 3
        app.toggle_group(url)
        assert table.row_count == 6
        app.action_clear()
        assert all(state.last is None and not state.baseline_set for state in app.states.values())
    assert all(session.closed for session in app.sessions.values())


@pytest.mark.parametrize("error", [None, "connection failed"])
def test_logs_identify_entry_and_destination(caplog, error):
    target = Target("target:0:1", "https://origin.test/api", "front-door", ConnectTo("edge.test", 443))
    result = make_result(url=target.url, error=error, http_code=None if error else 200)
    with caplog.at_level(logging.INFO):
        pingwatch.log_result(logging.getLogger("route-logs"), result, bool(error), 3, target)
    assert "front-door: https://origin.test/api" in caplog.text
    assert "entry=target:0:1" in caplog.text
    assert "connect_to=edge.test:443" in caplog.text
