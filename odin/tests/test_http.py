"""Tests for the HTTP layer: the JSON content-type guard and the API contract.

The guard exists because `text/plain` is a CORS-"simple" content type: a
browser sends such a cross-origin POST with no preflight, so any page the
operator visits can fire a state-changing request at this service. Requiring
application/json forces a preflight, which is never granted here.

These tests drive a real aiohttp application through its HTTP interface
rather than calling the handler functions, so they cover the routing and the
aiohttp content-type parsing too.
"""

from __future__ import annotations

import json

import pytest
import server

# (aiohttp.web is not referenced directly; TestServer provides the app)
from aiohttp.test_utils import TestClient, TestServer

# ---------------------------------------------------------------------------
# A WebServer with no camera, no go2rtc and no flight controller
# ---------------------------------------------------------------------------

@pytest.fixture
def webapp(tmp_path, monkeypatch):
    """A real aiohttp app serving the real handlers.

    BASE_DIR is redirected to a temp dir so a test can never write to the
    repo's camera.env, and the camera endpoints see a missing camera.env
    exactly like a fresh install with no HUD panel changes yet.
    """
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "telemetry").mkdir()

    state = server.TelemetryState(batt_cells=3)
    srv = server.WebServer(state, "127.0.0.1", 0, sources=None)
    return srv


async def _client(srv) -> TestClient:
    client = TestClient(TestServer(srv.build_app()))
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# The guard: non-JSON bodies are refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/camera", "/api/source"])
@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",                                  # CORS-simple: the hole
        "application/x-www-form-urlencoded",           # also CORS-simple
        "multipart/form-data",                         # also CORS-simple
        "",                                            # no header at all
    ],
)
async def test_non_json_post_is_rejected(webapp, path, content_type):
    client = await _client(webapp)
    try:
        headers = {"Content-Type": content_type} if content_type else {}
        resp = await client.post(path, data=b'{"mode":"sim","reset":true}',
                                 headers=headers)
        # 415 Unsupported Media Type. The important part is that it is not
        # 200/400-from-the-handler: the request must never reach validation,
        # let alone the state change.
        assert resp.status == 415, f"{path} accepted {content_type!r}"
        body = await resp.json()
        assert body["ok"] is False
        assert "application/json" in body["error"]
    finally:
        await client.close()


async def test_guard_runs_before_side_effects(webapp, tmp_path):
    """A rejected request must not have written camera.env."""
    client = await _client(webapp)
    try:
        resp = await client.post("/api/camera", data=b'{"reset":true}',
                                 headers={"Content-Type": "text/plain"})
        assert resp.status == 415
        assert not (tmp_path / "telemetry" / "camera.env").exists()
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Legitimate JSON still reaches the handler
# ---------------------------------------------------------------------------

async def test_json_body_is_accepted_and_validated(webapp):
    client = await _client(webapp)
    try:
        # A valid JSON object with a bad key: the handler's own validation
        # answers, which proves the guard let it through.
        resp = await client.post("/api/camera",
                                 json={"bogus_control": 1})
        assert resp.status == 400
        body = await resp.json()
        assert "unknown control" in body["error"]
    finally:
        await client.close()


async def test_json_with_charset_parameter_is_accepted(webapp):
    """Browsers often send 'application/json; charset=utf-8'."""
    client = await _client(webapp)
    try:
        resp = await client.post(
            "/api/camera", data=b'{"bogus_control":1}',
            headers={"Content-Type": "application/json; charset=utf-8"})
        assert resp.status == 400            # reached validation, not 415
        assert "unknown control" in (await resp.json())["error"]
    finally:
        await client.close()


async def test_valid_camera_control_is_written(webapp, tmp_path):
    """The happy path still persists a setting."""
    client = await _client(webapp)
    try:
        resp = await client.post("/api/camera", json={"saturation": "1.2"})
        # 200, or 500 if go2rtc is not running: either way the file is written.
        env = (tmp_path / "telemetry" / "camera.env")
        assert env.exists(), f"camera.env not written (status {resp.status})"
        assert "CAM_SATURATION=1.2" in env.read_text()
    finally:
        await client.close()


async def test_source_toggle_without_manager_is_503(webapp):
    """No SourceManager means the endpoint says so instead of pretending."""
    client = await _client(webapp)
    try:
        resp = await client.post("/api/source", json={"mode": "sim"})
        assert resp.status == 503
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Read-only endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/state", "/health", "/api/source"])
async def test_get_endpoints_work_without_a_flight_controller(webapp, path):
    client = await _client(webapp)
    try:
        resp = await client.get(path)
        assert resp.status == 200
    finally:
        await client.close()


async def test_health_shape(webapp):
    client = await _client(webapp)
    try:
        body = await (await client.get("/health")).json()
        # /health is what the operator's monitoring hits; keep the contract small.
        assert set(body) >= {"ok", "source", "link_ok", "uptime_s"}
    finally:
        await client.close()


async def test_state_reports_no_flight_controller_honestly(webapp):
    client = await _client(webapp)
    try:
        body = await (await client.get("/api/state")).json()
        assert body["link_ok"] is False
        assert body["source"] == "none"
        # Unknown values must be null, which the HUD draws as "---". A 0 here
        # would be displayed as a real reading.
        for key in ("hdop", "alt_rel", "home_dist", "home_bearing",
                    "flight_time_s", "mah_used", "batt_cell_v"):
            assert body.get(key) is None, f"{key} should be unknown, got {body.get(key)!r}"
    finally:
        await client.close()


async def test_no_control_endpoint_exists(webapp):
    """There is deliberately no way to command the aircraft from HTTP.

    This test is the tripwire for the day someone adds /api/goto or an arm
    endpoint: it fails, and the reviewer has to think about the safety gate
    described in the README before merge.
    """
    client = await _client(webapp)
    try:
        for path in ("/api/goto", "/api/arm", "/api/mode", "/api/command",
                     "/api/param"):
            resp = await client.post(path, json={"x": 1})
            assert resp.status == 404, f"{path} exists but must not"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# WebSocket: only ping is understood
# ---------------------------------------------------------------------------

async def test_ws_accepts_ping_and_echoes_it(webapp):
    client = await _client(webapp)
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"ping": 123.5})
        msg = await ws.receive(timeout=5)
        assert json.loads(msg.data) == {"pong": 123.5}
        await ws.close()
    finally:
        await client.close()


async def test_ws_ignores_unknown_messages(webapp):
    """Nothing inbound is interpreted as a command; junk must not crash it."""
    client = await _client(webapp)
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"arm": True})
        await ws.send_str("not json at all")
        await ws.send_json({"ping": 7})
        msg = await ws.receive(timeout=5)
        # The socket is still alive and only the ping produced a reply.
        assert json.loads(msg.data) == {"pong": 7}
        await ws.close()
    finally:
        await client.close()
