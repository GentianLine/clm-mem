"""End-to-end tests for the web studio API, MCP endpoint, workspace sandboxes
and the remote attach client (`clm-mem chat --connect`). Mock backends only.
"""

import json
import os
import urllib.error
import urllib.request
import pytest
from clm_mem import start_supervisor


def http_call(sup, path, method="GET", body=None, headers=None):
    url = f"{sup.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    raw = resp.read().decode()
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


HDR = {"X-Cllm-Mem-Session": "ws:proj"}


@pytest.fixture()
def sup(tmp_path):
    s = start_supervisor(
        public_port=0,
        served_model="mock-model",
        backend_mode="mock",
        systemone_mode="mock",
        harness_kwargs={
            "workdir": str(tmp_path),
            "max_context_tokens": 600,
            # mock backstop noul sits at 0.6/0.85; allow eviction
            "systemone_evict_threshold": 0.5,
            # agent surface on, offline providers only (chat-attribution tests)
            "tools_enabled": True,
            "search_provider": "stub",
            "sandbox_mode": "jail",
        },
        timeout=30,
    )
    yield s
    s.shutdown()


# -- web studio assets -----------------------------------------------------
def test_ui_assets_served(sup):
    st, body = http_call(sup, "/")
    assert st == 200 and "clm-mem" in str(body)
    for asset in (
        "/style.css",
        "/app.js",
        "/favicon.svg",
        "/dyson.svg",
        "/dyson-done.svg",
        "/dyson.js",
    ):
        st, _ = http_call(sup, asset)
        assert st == 200


def test_ui_disabled(tmp_path):
    s = start_supervisor(
        public_port=0,
        backend_mode="none",
        systemone_mode="none",
        ui=False,
        timeout=30,
        harness_kwargs={"workdir": str(tmp_path)},
    )
    try:
        st, body = http_call(s, "/")
        assert st == 404 and s.ui_url is None
    finally:
        s.shutdown()


# -- workspaces --------------------------------------------------------------
def test_workspace_lifecycle(sup):
    st, d = http_call(sup, "/v1/clm/workspaces")
    assert st == 200 and d["workspaces"] == []

    st, ws = http_call(sup, "/v1/clm/workspaces", "POST", {"name": "proj"})
    assert st == 200 and ws["name"] == "proj"

    st, d = http_call(sup, "/v1/clm/workspaces")
    assert len(d["workspaces"]) == 1
    assert d["workspaces"][0]["active"] is False

    http_call(
        sup,
        "/v1/chat/completions",
        "POST",
        {"messages": [{"role": "user", "content": "Remember: key KEY-7 port 8077"}]},
        HDR,
    )
    st, d = http_call(sup, "/v1/clm/workspaces")
    w = d["workspaces"][0]
    assert w["active"] and w["turn"] == 1 and w["counts"]["longterm"] >= 1

    # sandbox files are isolated per workspace
    root = os.path.join(sup.config.harness_kwargs["workdir"], "workspaces", "proj")
    assert os.path.isfile(os.path.join(root, "context.jsonl"))

    st, d = http_call(sup, "/v1/clm/workspaces/proj", "DELETE")
    assert st == 200 and d["deleted"] == "proj"
    assert not os.path.exists(root)


def test_workspace_name_validation(sup):
    for bad in ("../evil", "", "a/b", ".hidden"):
        st, _ = http_call(sup, "/v1/clm/workspaces", "POST", {"name": bad})
        assert st == 400, f"name {bad!r} should be rejected"


# -- snapshot / blocks / archive / settings ------------------------------------
def test_snapshot_block_archive_reinject_settings(sup):
    big = "z" * 800  # longer than the 320-char preview
    http_call(
        sup,
        "/v1/chat/completions",
        "POST",
        {"messages": [{"role": "user", "content": f"Remember: id VAULT-9 {big}"}]},
        HDR,
    )

    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aproj/snapshot")
    assert st == 200 and snap["turn"] == 1
    long_ones = [
        b for b in snap["blocks"] if b["tag"] == "longterm" and b["role"] == "user"
    ]
    assert any(b["truncated"] and len(b["content"]) == 320 for b in long_ones)

    bid = next(
        b["id"] for b in snap["blocks"] if b["truncated"] and b["role"] == "user"
    )
    st, full = http_call(sup, f"/v1/clm/sessions/ws%3Aproj/block/{bid}")
    assert st == 200 and len(full["block"]["content"]) > 700

    # settings round-trip
    st, d = http_call(
        sup, "/v1/clm/sessions/ws%3Aproj/settings", "POST", {"shortterm_keep_pct": 0.8}
    )
    assert st == 200 and d["changed"]["shortterm_keep_pct"] == 0.8
    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aproj/snapshot")
    assert snap["shortterm_keep_pct"] == 0.8

    # bad settings
    st, _ = http_call(
        sup, "/v1/clm/sessions/ws%3Aproj/settings", "POST", {"shortterm_keep_pct": 7}
    )
    assert st == 400


def test_archive_search_and_reinject_flow(sup):
    filler = "chatter filler that eats tokens " * 20
    hdr = {"X-Cllm-Mem-Session": "ws:s2"}
    for i in range(6):
        http_call(
            sup,
            "/v1/chat/completions",
            "POST",
            {"messages": [{"role": "user", "content": f"Quick note {i}: {filler}"}]},
            hdr,
        )
    st, d = http_call(sup, "/v1/clm/sessions/ws%3As2/archive?q=chatter&limit=5")
    assert st == 200 and len(d["hits"]) >= 1
    bid = d["hits"][0]["id"]

    st, r = http_call(
        sup, "/v1/clm/sessions/ws%3As2/reinject", "POST", {"block_id": bid}
    )
    assert st == 200 and r["reinstated"]

    # reinject of unknown id -> 404
    st, _ = http_call(
        sup, "/v1/clm/sessions/ws%3As2/reinject", "POST", {"block_id": 99999}
    )
    assert st == 404


def test_unknown_session_404s(sup):
    for path in (
        "/v1/clm/sessions/nobody/snapshot",
        "/v1/clm/sessions/nobody/archive?q=x",
        "/v1/clm/sessions/nobody/reinject",
    ):
        method = "GET" if "reinject" not in path else "POST"
        st, _ = http_call(sup, path, method, {} if method == "POST" else None)
        assert st == 404


# -- MCP endpoint ------------------------------------------------------------
def mcp_call(sup, msg):
    return http_call(sup, "/mcp", "POST", msg)


def test_mcp_initialize_and_tools(sup):
    st, init = mcp_call(
        sup, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert st == 200 and init["result"]["serverInfo"]["name"] == "clm-mem"
    assert "protocolVersion" in init["result"]

    st, lst = mcp_call(sup, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert {"clm_chat", "clm_recall", "clm_workspaces"} <= names

    st, resp = mcp_call(
        sup, {"jsonrpc": "2.0", "id": 3, "method": "bogus"}
    )  # notification-ish
    assert st == 200 and resp["error"]["code"] == -32601


def test_mcp_tool_calls_drive_same_library(sup):
    st, r = mcp_call(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "clm_chat",
                "arguments": {
                    "message": "Remember: MCP fact ZQ-3 port 8093",
                    "workspace": "mcpw",
                },
            },
        },
    )
    assert st == 200 and r["result"]["isError"] is False
    assert r["result"]["content"][0]["text"].strip()

    # same workspace visible via REST, i.e. MCP drives the identical harness
    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Amcpw/snapshot")
    assert st == 200 and snap["turn"] == 1

    st, r = mcp_call(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "clm_status", "arguments": {"workspace": "mcpw"}},
        },
    )
    assert json.loads(r["result"]["content"][0]["text"])["turn"] == 1

    st, r = mcp_call(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "clm_set_keep_pct",
                "arguments": {"pct": 0.9, "workspace": "mcpw"},
            },
        },
    )
    assert r["result"]["isError"] is False

    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Amcpw/snapshot")
    assert snap["shortterm_keep_pct"] == 0.9


def test_mcp_notification_accepted_202(sup):
    st, _ = mcp_call(sup, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202


# -- remote attach client ------------------------------------------------------
def test_remote_harness_matches_server(sup):
    from clm_mem.remote import connect

    h = connect(f"{sup.public_base}", workspace="remote")
    reply = h.step("Remember: remote fact RK-1 port 8051")
    assert reply
    assert "turn=1" in h.status()

    # live knob round-trips through settings endpoint
    h.cfg.shortterm_keep_pct = 0.7
    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aremote/snapshot")
    assert snap["shortterm_keep_pct"] == 0.7

    blocks = h.live_blocks()
    assert blocks and all(hasattr(b, "tag") for b in blocks)
    assert len(h.live_blocks("longterm")) >= 1

    hits = h.recall("remote fact")
    # not necessarily evicted yet; search must still work (possibly empty)
    assert isinstance(hits, list)

    assert h.ui_url() == sup.base_url


def test_remote_step_header_used(sup):
    from clm_mem.remote import RemoteHarness

    h = RemoteHarness(f"{sup.public_base}", session="ws:hdrcheck")
    h.step("hello there")
    st, lst = http_call(sup, "/v1/clm/sessions")
    assert "ws:hdrcheck" in lst["sessions"]


# -- forgetting-strategy toggles (CLI flags ride the same settings route) ------
def test_settings_forget_toggles(sup):
    http_call(
        sup,
        "/v1/chat/completions",
        "POST",
        {"messages": [{"role": "user", "content": "hi there"}]},
        HDR,
    )

    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aproj/snapshot")
    assert snap["decay_enabled"] is False and snap["supersede_enabled"] is False

    st, d = http_call(
        sup, "/v1/clm/sessions/ws%3Aproj/settings", "POST", {"decay_enabled": True}
    )
    assert st == 200 and d["changed"]["decay_enabled"] is True

    # stringy truthy/falsey accepted (coerced like other knobs)
    st, d = http_call(
        sup,
        "/v1/clm/sessions/ws%3Aproj/settings",
        "POST",
        {"supersede_enabled": "on", "decay_enabled": "false"},
    )
    assert d["changed"]["supersede_enabled"] is True
    assert d["changed"]["decay_enabled"] is False

    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aproj/snapshot")
    assert snap["decay_enabled"] is False and snap["supersede_enabled"] is True


def test_mcp_set_forget_tool(sup):
    st, lst = mcp_call(sup, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert "clm_set_forget" in names

    st, r = mcp_call(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "clm_set_forget",
                "arguments": {"workspace": "fgw", "decay": True, "supersede": True},
            },
        },
    )
    assert st == 200 and r["result"]["isError"] is False

    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Afgw/snapshot")
    assert snap["decay_enabled"] is True and snap["supersede_enabled"] is True

    # omitted flags read back current state instead of changing it
    st, r = mcp_call(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "clm_set_forget", "arguments": {"workspace": "fgw"}},
        },
    )
    text = r["result"]["content"][0]["text"]
    assert "decay_enabled=True" in text and "supersede_enabled=True" in text


def test_remote_cfg_forget_toggles(sup):
    """The remote attach client exposes the same toggles as local.cfg."""
    from clm_mem.remote import RemoteHarness

    h = RemoteHarness(f"{sup.public_base}", session="ws:remfg")
    h.step("hello there")  # creates the session
    h.prime()
    assert h.cfg.decay_enabled is False and h.cfg.supersede_enabled is False
    h.cfg.decay_enabled = True  # pushed via /settings
    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Aremfg/snapshot")
    assert snap["decay_enabled"] is True


# -- chats: transcript slots inside a workspace session ------------------------
HDR2 = {"X-Cllm-Mem-Session": "ws:chats", "X-Cllm-Mem-Chat": "chat-001"}


def _send(sup, text, hdr):
    return http_call(
        sup,
        "/v1/chat/completions",
        "POST",
        {"model": "mock-model", "messages": [{"role": "user", "content": text}]},
        hdr,
    )


def test_chat_slots_partition_transcripts(sup):
    http_call(sup, "/v1/clm/workspaces", "POST", {"name": "chats"})
    st, _ = _send(sup, "Remember: alpha fact AF-1 port 8001", HDR2)
    assert st == 200

    hdr2b = {"X-Cllm-Mem-Session": "ws:chats", "X-Cllm-Mem-Chat": "chat-002"}
    _send(sup, "Remember: beta fact BF-2 port 8002", hdr2b)

    # unfiltered snapshot sees both chats
    st, snap = http_call(sup, "/v1/clm/sessions/ws%3Achats/snapshot")
    turns = {b["chat"] for b in snap["blocks"]}
    assert {"chat-001", "chat-002"} <= turns, turns

    # chat-filtered snapshot only carries that chat's blocks
    st, snap1 = http_call(sup, "/v1/clm/sessions/ws%3Achats/snapshot?chats=chat-001")
    assert all(b["chat"] == "chat-001" for b in snap1["blocks"])
    assert any("alpha" in b["content"] for b in snap1["blocks"])
    assert not any("beta" in b["content"] for b in snap1["blocks"])

    st, snap2 = http_call(sup, "/v1/clm/sessions/ws%3Achats/snapshot?chats=chat-002")
    assert all(b["chat"] == "chat-002" for b in snap2["blocks"])

    # the workspace listing carries the chat slots for the sidebar
    st, ws = http_call(sup, "/v1/clm/workspaces")
    w = [x for x in ws["workspaces"] if x["name"] == "chats"][0]
    assert "chat-001" in w["chats"] and "chat-002" in w["chats"]

    # snapshot also reports the chat inventory
    assert set(snap["chats"]) >= {"chat-001", "chat-002"}


def test_chat_open_registers_empty_slot(sup):
    http_call(sup, "/v1/clm/workspaces", "POST", {"name": "chats"})
    st, d = http_call(
        sup,
        "/v1/clm/sessions/ws%3Achats/chat-open",
        "POST",
        {"chat": "chat-005"},
    )
    assert st == 200 and d["ok"]
    st, ws = http_call(sup, "/v1/clm/workspaces")
    w = [x for x in ws["workspaces"] if x["name"] == "chats"][0]
    assert "chat-005" in w["chats"]

    st, _ = http_call(
        sup,
        "/v1/clm/sessions/ws%3Achats/chat-open",
        "POST",
        {"chat": "evil/../x"},
    )
    assert st == 400


def test_chat_activity_filtering(sup):
    http_call(sup, "/v1/clm/workspaces", "POST", {"name": "chats"})
    st, d = http_call(
        sup,
        "/v1/clm/sessions/ws%3Achats/search",
        "POST",
        {"query": "hello world", "max_results": 2, "chat": "chat-001"},
        HDR2,
    )
    assert st == 200 and len(d["hits"]) == 2

    st, acts = http_call(
        sup, "/v1/clm/sessions/ws%3Achats/activity?chats=chat-001", headers=HDR2
    )
    chats = {a.get("chat") for a in acts["activity"]}
    assert chats == {"chat-001"}, chats

    st, acts2 = http_call(
        sup, "/v1/clm/sessions/ws%3Achats/activity?chats=chat-002", headers=HDR2
    )
    assert acts2["activity"] == []  # nothing happened in chat-002


def test_reset_keeps_archive_and_chat_state(sup):
    http_call(sup, "/v1/clm/workspaces", "POST", {"name": "chats"})
    hdr = {
        "X-Cllm-Mem-Session": "ws:chats",
        "X-Cllm-Mem-Chat": "chat-001",
    }
    _send(sup, "Remember: gamma fact GG-3 port 8003", hdr)

    filler = "chatter filler that eats tokens " * 20
    for _ in range(6):
        _send(sup, filler, hdr)

    st, snap1 = http_call(sup, "/v1/clm/sessions/ws%3Achats/snapshot?chats=chat-001")
    assert snap1["turn"] > 1

    st, r = http_call(
        sup,
        "/v1/clm/sessions/ws%3Achats/reset",
        "POST",
        {"chats": ["chat-002"]},
    )
    assert st == 200 and r["existed"] is True

    # a fresh session for chat-002 starts empty of chat-001's transcript
    hdr2 = {
        "X-Cllm-Mem-Session": "ws:chats",
        "X-Cllm-Mem-Chat": "chat-002",
    }
    _send(sup, "Remember: delta fact DD-4 port 8004", hdr2)
    st, snap2 = http_call(sup, "/v1/clm/sessions/ws%3Achats/snapshot?chats=chat-002")
    assert any("delta" in b["content"] for b in snap2["blocks"])
    assert all(b["chat"] == "chat-002" for b in snap2["blocks"])
