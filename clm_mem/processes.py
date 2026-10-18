"""Process management: launch backend servers, wait until they answer,
shut them down cleanly. Used by `clm-mem serve` so the whole stack looks like
one llama.cpp endpoint to clients while being 2-3 background processes.

Nothing here downloads or compiles anything -- it only ever execs binaries or
modules you point it at (or the bundled mocks).
"""
from __future__ import annotations
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


def free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an unused TCP port (bind to 0 and read it back)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _tail(path: str, n: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(n)
        return data.decode(errors="replace")[-n:]
    except OSError:
        return "(no log)"


@dataclass
class ProcessHandle:
    name: str
    proc: subprocess.Popen
    host: str
    port: int
    log_path: str = ""
    log_fh: object | None = field(default=None, repr=False)

    @property
    def root_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        return f"{self.root_url}/v1"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class ProcessManager:
    """Launches HTTP-servicing subprocesses, waits for readiness, shuts down."""

    def __init__(self, log_dir: Optional[str] = None):
        self.log_dir = log_dir or tempfile.mkdtemp(prefix="clm-mem-logs-")
        os.makedirs(self.log_dir, exist_ok=True)
        self.handles: List[ProcessHandle] = []

    # -- launching --------------------------------------------------------
    def start(self, name: str, argv: Sequence[str], *, host: str = "127.0.0.1",
              port: Optional[int] = None,
              ready_paths: Sequence[str] = ("/health", "/v1/models"),
              timeout: float = 90.0, env: Optional[dict] = None,
              cwd: Optional[str] = None) -> ProcessHandle:
        """Start one server process and poll until it answers over HTTP."""
        argv = list(argv)
        if port is None:
            port = free_port(host)
        log_path = os.path.join(self.log_dir, f"{name}.log")
        fh = open(log_path, "wb")
        penv = dict(os.environ)
        penv.update(env or {})
        penv.setdefault("PYTHONUNBUFFERED", "1")
        penv["CLM_MEM_HOST"] = host
        penv["CLM_MEM_PORT"] = str(port)
        try:
            proc = subprocess.Popen(argv, stdout=fh,
                                    stderr=subprocess.STDOUT, env=penv, cwd=cwd)
        except Exception:
            fh.close()
            raise
        handle = ProcessHandle(name=name, proc=proc, host=host, port=port,
                               log_path=log_path, log_fh=fh)
        try:
            self._wait_ready(handle, ready_paths, timeout)
        except Exception:
            self._terminate(handle)
            raise
        self.handles.append(handle)
        return handle

    def _wait_ready(self, h: ProcessHandle, paths: Sequence[str],
                    timeout: float) -> None:
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            if h.proc.poll() is not None:
                raise RuntimeError(
                    f"{h.name} exited early (rc={h.proc.returncode}):\n"
                    f"{_tail(h.log_path)}")
            for path in paths:
                url = f"{h.root_url}{path}"
                try:
                    with urllib.request.urlopen(url, timeout=1.0) as resp:
                        if resp.status < 500:
                            resp.read()
                            return
                except urllib.error.HTTPError as e:      # alive but 4xx = ready
                    return
                except Exception as exc:
                    last_err = f"{url}: {exc}"
            time.sleep(0.15)
        raise RuntimeError(
            f"{h.name} did not become ready on {h.root_url} within "
            f"{timeout}s (last: {last_err}):\n{_tail(h.log_path)}")

    # -- shutdown -----------------------------------------------------------
    def _terminate(self, h: ProcessHandle) -> None:
        try:
            if h.proc.poll() is None:
                h.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):                      # up to ~2s graceful stop
            if h.proc.poll() is not None:
                break
            time.sleep(0.1)
        try:
            if h.proc.poll() is None:
                h.proc.kill()
                h.proc.wait(timeout=5)
        except Exception:
            pass
        if h.log_fh:
            try:
                h.log_fh.close()
            except Exception:
                pass
            h.log_fh = None

    def shutdown(self) -> None:
        for h in reversed(self.handles):
            self._terminate(h)
        self.handles.clear()

    # -- convenience ---------------------------------------------------------
    def mock_argv(self, kind: str) -> List[str]:
        """Command line that runs one of the bundled mock backends."""
        return [sys.executable, "-m", "clm_mem.mock_backends",
                "--kind", kind]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def render_flags(flags: Sequence[str], host: str, port: int) -> List[str]:
    """Expand ``{host}`` / ``{port}`` placeholders in a flag template."""
    out = []
    for f in flags:
        if "{host}" in f or "{port}" in f:
            out.append(f.format(host=host, port=port))
        else:
            out.append(f)
    return out
