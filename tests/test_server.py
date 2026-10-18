"""End-to-end tests for `clm-mem serve`: proxy + mock backends as real
subprocesses over localhost HTTP. No GPU, no binaries, no downloads.
"""
import json
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
    except urllib.error.HTTPError as e:      # we want the error body too
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


def sse_call(sup, path, body, headers=None):
    req = urllib.request.Request(f"{sup.base_url}{path}",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=30)
    assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
    text, done = "", False
    for raw_line in resp:
        line = raw_line.decode().strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "":
            continue
        if payload == "[DONE]":
            done = True
            break
        obj = json.loads(payload)
        delta = obj["choices"][0]["delta"]
        text += delta.get("content", "") or ""
    resp.close()
    return text, done


@pytest.fixture()
def sup(tmp_path):
    s = start_supervisor(
        public_port=0,
        served_model="mock-model",
        backend_mode="mock",
        systemone_mode="mock",
        harness_kwargs={"workdir": str(tmp_path),
                        "max_context_tokens": 600,
                        "memory_preamble": True},
        timeout=30)
    yield s
    s.shutdown()


# -- llama.cpp-shaped surface ---------------------------------------------------
def test_health_version_models(sup):
    st, health = http_call(sup, "/health")
    assert st == 200 and health["status"] == "ok"
    assert health["model"] == "mock-model"
    assert set(health["processes"]) >= {"backend-mock", "systemone-mock"}

    st, ver = http_call(sup, "/version")
    assert st == 200 and "llama.cpp" in ver

    st, models = http_call(sup, "/v1/models")
    ids = [m["id"] for m in models["data"]]
    assert "mock-model" in ids


def test_native_props_passthrough(sup):
    st, props = http_call(sup, "/props")
    assert st == 200 and props.get("model_type") == "mock"


def test_options_cors(sup):
    req = urllib.request.Request(f"{sup.base_url}/v1/chat/completions",
                                 method="OPTIONS")
    resp = urllib.request.urlopen(req, timeout=30)
    assert resp.status == 204
    assert "X-Cllm-Mem-Session" in (resp.headers.get(
        "Access-Control-Allow-Headers") or "")

# -- chat: passthrough and memory-managed ---------------------------------------
def test_chat_passthrough_without_session(sup):
    st, resp = http_call(sup, "/v1/chat/completions", "POST",
                         {"model": "mock-model",
                          "messages": [{"role": "user",
                                        "content": "hello there"}]})
    assert st == 200
    assert resp["choices"][0]["message"]["content"]
    assert "clm_mem" not in resp          # plain llama.cpp clients unchanged


def test_chat_streaming_passthrough(sup):
    text, done = sse_call(sup, "/v1/chat/completions",
                          {"model": "mock-model", "stream": True,
                           "messages": [{"role": "user",
                                         "content": "stream me please"}]})
    assert done and text


def test_chat_session_memory_and_status(sup):
    hdr = {"X-Cllm-Mem-Session": "alice"}
    st, resp = http_call(
        sup, "/v1/chat/completions", "POST",
        {"model": "mock-model",
         "messages": [{"role": "user",
                       "content": "Remember: API key KEY-9 port 8090"}]}, hdr)
    assert st == 200
    assert resp["choices"][0]["message"]["content"]
    assert resp["clm_mem"]["session_id"] == "alice"

    st, status = http_call(sup, "/v1/clm/sessions/alice/status")
    assert st == 200
    assert status["turns"] == 1
    assert status["counts"]["longterm"] >= 1      # fact survived as longterm
    assert status["tokens"] <= 600                # budget respected

    # second turn still works and reuses the same harness
    st, resp2 = http_call(
        sup, "/v1/chat/completions", "POST",
        {"model": "mock-model",
         "messages": [{"role": "user",
                       "content": "what was my API key? remember it"}]}, hdr)
    assert st == 200 and resp2["choices"][0]["message"]["content"]

    st, status = http_call(sup, "/v1/clm/sessions/alice/status")
    assert status["turns"] == 2
    assert status["tokens"] <= 600


def test_chat_session_streaming(sup):
    hdr = {"X-Cllm-Mem-Session": "streamer"}
    text, done = sse_call(
        sup, "/v1/chat/completions",
        {"model": "mock-model", "stream": True,
         "messages": [{"role": "user",
                       "content": "Remember: codename Aurora port 8042"}]}, hdr)
    assert done and text
    st, status = http_call(sup, "/v1/clm/sessions/streamer/status")
    assert st == 200 and status["turns"] == 1


def test_session_list_reset_delete(sup):
    hdr = {"X-Cllm-Mem-Session": "bob"}
    http_call(sup, "/v1/chat/completions", "POST",
              {"model": "mock-model",
               "messages": [{"role": "user", "content": "hi there"}]}, hdr)

    st, lst = http_call(sup, "/v1/clm/sessions")
    assert st == 200 and "bob" in lst["sessions"]

    st, msg = http_call(sup, "/v1/clm/sessions/bob/reset", "POST")
    assert st == 200 and msg["reset"] == "bob"

    st, _ = http_call(sup, "/v1/clm/sessions/bob/status")
    assert st == 404                                  # reset dropped it

    http_call(sup, "/v1/chat/completions", "POST",
              {"model": "mock-model",
               "messages": [{"role": "user", "content": "hi again"}]}, hdr)
    st, msg = http_call(sup, "/v1/clm/sessions/bob", "DELETE")
    assert st == 200 and msg["deleted"] == "bob"


def test_unknown_session_status_404(sup):
    st, _ = http_call(sup, "/v1/clm/sessions/nobody/status")
    assert st == 404

# -- systemone proxy -------------------------------------------------------------
def test_systemone_proxied_through_public_port(sup):
    st, resp = http_call(
        sup, "/v1/systemone", "POST",
        {"state": {"text": "Remember: password hunter2"},
         "questions": {"duration": {
             "type": "choice",
             "instructions": "tag this block",
             "criteria": {"longterm": "durable", "shortterm": "working",
                          "transient": "chatter"}}}})
    assert st == 200
    assert resp["answers"]["duration"]["choice"] == "longterm"

# -- process lifecycle -------------------------------------------------------------
def test_supervisor_spawns_and_kills_processes(tmp_path):
    s = start_supervisor(public_port=0, backend_mode="mock",
                         systemone_mode="mock", timeout=30,
                         harness_kwargs={"workdir": str(tmp_path)})
    try:
        pids = [h.proc.pid for h in s.manager.handles]
        assert len(pids) == 2 and all(h.alive() for h in s.manager.handles)
    finally:
        s.shutdown()
    # everything must be reaped, including the proxy's own threads
    import os
    import time
    for pid in pids:
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:        # gone (or never existed) -> good
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"pid {pid} survived shutdown")
    assert s.manager.handles == []


def test_external_backend_mode(tmp_path):
    """backend_mode='external' proxies a server we did not spawn."""
    from clm_mem.processes import ProcessManager
    mgr = ProcessManager()
    try:
        backend = mgr.start("ext-backend", mgr.mock_argv("llm"), timeout=30)
        s = start_supervisor(public_port=0, served_model="mock-model",
                             backend_mode="external",
                             backend_url=f"{backend.base_url}",   # /v1 gets stripped
                             systemone_mode="mock", timeout=30,
                             harness_kwargs={"workdir": str(tmp_path)})
        try:
            st, resp = http_call(s, "/v1/chat/completions", "POST",
                                 {"model": "mock-model",
                                  "messages": [{"role": "user",
                                                "content": "external hello"}]})
            assert st == 200 and resp["choices"][0]["message"]["content"]
        finally:
            s.shutdown()
    finally:
        mgr.shutdown()


def test_spawn_requires_command():
    with pytest.raises(ValueError):
        start_supervisor(public_port=0, backend_mode="spawn", timeout=5)


def test_backend_none_mode_errors_cleanly(tmp_path):
    s = start_supervisor(public_port=0, backend_mode="none",
                         systemone_mode="none", timeout=30,
                         harness_kwargs={"workdir": str(tmp_path)})
    try:
        st, body = http_call(s, "/v1/chat/completions", "POST",
                             {"model": "x",
                              "messages": [{"role": "user",
                                            "content": "hello"}]})
        assert st == 503
    finally:
        s.shutdown()
