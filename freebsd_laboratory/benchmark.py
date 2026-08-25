from __future__ import annotations

import argparse
import asyncio
import csv
import http.cookiejar
import io
import json
import math
import os
import platform
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import tornado.httpclient
import tornado.websocket

from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient, RuntimeControlError


RUNTIME_PRESETS: dict[str, dict[str, Any]] = {
    "jail": {
        "alias": "jail",
        "name": "freebsd-python",
        "display_name": "FreeBSD VNET Jail",
        "guest_os": "FreeBSD",
        "provisioner": "freebsd-jail-provisioner",
        "profile": None,
        "type": "jail",
    },
    "freebsd-bhyve": {
        "alias": "freebsd-bhyve",
        "name": "freebsd-python-bhyve",
        "display_name": "FreeBSD bhyve VM",
        "guest_os": "FreeBSD",
        "provisioner": "freebsd-bhyve-provisioner",
        "profile": "freebsd-python",
        "type": "bhyve",
    },
    "linux-bhyve": {
        "alias": "linux-bhyve",
        "name": "linux-python-bhyve",
        "display_name": "Linux bhyve VM",
        "guest_os": "Linux",
        "provisioner": "linux-bhyve-provisioner",
        "profile": "linux-python",
        "type": "bhyve",
    },
}

DEFAULT_PROBE_CODE = (
    "import sys, os, platform, time\n"
    "info = {\n"
    "    'platform': sys.platform,\n"
    "    'nodename': os.uname().nodename,\n"
    "    'system': platform.system(),\n"
    "    'release': platform.release(),\n"
    "    'machine': platform.machine(),\n"
    "    'python': sys.version.split()[0],\n"
    "}\n"
    "print('BENCHMARK_PROBE:' + json.dumps(info))\n"
)


@dataclass(frozen=True)
class BenchmarkConfig:
    mode: str = "jupyter"  # "jupyter", "daemon", "both"
    runtimes: list[str] = field(default_factory=lambda: ["jail", "freebsd-bhyve", "linux-bhyve"])
    iterations: int = 3
    warmup: int = 0
    base_url: str = "http://127.0.0.1:8888"
    token: str = ""
    socket_path: str = DEFAULT_RUNTIME_SOCKET
    timeout: float = 90.0
    output_format: str = "table"  # "table", "markdown", "json", "csv"
    output_file: str | None = None
    verbose: bool = False
    quiet: bool = False


@dataclass
class IterationResult:
    runtime_alias: str
    runtime_display: str
    iteration: int
    is_warmup: bool
    mode: str
    create_sec: float = 0.0
    ws_connect_sec: float = 0.0
    first_exec_sec: float = 0.0
    spinup_sec: float = 0.0
    warm_exec_sec: float = 0.0
    destroy_sec: float = 0.0
    lifecycle_sec: float = 0.0
    success: bool = True
    error: str | None = None
    kernel_id: str | None = None
    guest_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricSummary:
    count: int = 0
    mean: float = 0.0
    median: float = 0.0
    min: float = 0.0
    max: float = 0.0
    std_dev: float = 0.0
    p95: float = 0.0


@dataclass
class RuntimeBenchmarkSummary:
    runtime_alias: str
    runtime_display: str
    mode: str
    total_runs: int
    successful_runs: int
    failed_runs: int
    metrics: dict[str, MetricSummary] = field(default_factory=dict)
    guest_metadata: dict[str, Any] = field(default_factory=dict)


def compute_metric_summary(values: Sequence[float]) -> MetricSummary:
    if not values:
        return MetricSummary()
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mean_val = statistics.fmean(sorted_vals)
    median_val = statistics.median(sorted_vals)
    min_val = sorted_vals[0]
    max_val = sorted_vals[-1]
    std_dev_val = statistics.stdev(sorted_vals) if n > 1 else 0.0

    # 95th percentile with linear interpolation
    if n == 1:
        p95_val = sorted_vals[0]
    else:
        rank = 0.95 * (n - 1)
        lower = int(math.floor(rank))
        upper = int(math.ceil(rank))
        weight = rank - lower
        p95_val = sorted_vals[lower] * (1.0 - weight) + sorted_vals[upper] * weight

    return MetricSummary(
        count=n,
        mean=round(mean_val, 4),
        median=round(median_val, 4),
        min=round(min_val, 4),
        max=round(max_val, 4),
        std_dev=round(std_dev_val, 4),
        p95=round(p95_val, 4),
    )


def summarize_results(results: list[IterationResult]) -> list[RuntimeBenchmarkSummary]:
    grouped: dict[tuple[str, str], list[IterationResult]] = {}
    for r in results:
        if r.is_warmup:
            continue
        key = (r.runtime_alias, r.mode)
        grouped.setdefault(key, []).append(r)

    summaries: list[RuntimeBenchmarkSummary] = []
    for (alias, mode), group in grouped.items():
        successful = [r for r in group if r.success]
        display = group[0].runtime_display if group else alias
        guest_meta: dict[str, Any] = {}
        for r in successful:
            if r.guest_metadata:
                guest_meta = r.guest_metadata
                break

        metrics: dict[str, MetricSummary] = {}
        metric_names = [
            "create_sec",
            "destroy_sec",
            "lifecycle_sec",
        ]
        if mode == "jupyter":
            metric_names.extend([
                "ws_connect_sec",
                "first_exec_sec",
                "spinup_sec",
                "warm_exec_sec",
            ])

        for m_name in metric_names:
            vals = [getattr(r, m_name) for r in successful]
            metrics[m_name] = compute_metric_summary(vals)

        summaries.append(
            RuntimeBenchmarkSummary(
                runtime_alias=alias,
                runtime_display=display,
                mode=mode,
                total_runs=len(group),
                successful_runs=len(successful),
                failed_runs=len(group) - len(successful),
                metrics=metrics,
                guest_metadata=guest_meta,
            )
        )
    return summaries


class JupyterClientHelper:
    """Helper for interacting with JupyterLab REST and WebSocket channels."""

    def __init__(self, base_url: str = "http://127.0.0.1:8888", token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.ws_base_url = re.sub(r"^http", "ws", self.base_url)
        self.token = token
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.xsrf_token: str = ""
        self._authenticate()

    def _authenticate(self) -> None:
        """Fetch session cookies and XSRF token."""
        try:
            req_url = f"{self.base_url}/lab"
            if self.token:
                req_url += f"?token={self.token}"
            req = urllib.request.Request(req_url)
            self.opener.open(req, timeout=10)
        except Exception:
            try:
                req_url = f"{self.base_url}/api"
                req = urllib.request.Request(req_url)
                self.opener.open(req, timeout=10)
            except Exception:
                pass

        for cookie in self.cookie_jar:
            if cookie.name == "_xsrf":
                self.xsrf_token = cookie.value
                break

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        if self.xsrf_token:
            headers["X-XSRFToken"] = self.xsrf_token
        return headers

    def _build_cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.cookie_jar)

    def ping(self) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.base_url}/api/status", headers=self._build_headers())
        with self.opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_kernelspecs(self) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.base_url}/api/kernelspecs", headers=self._build_headers())
        with self.opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("kernelspecs", {})

    def create_kernel(self, kernel_name: str, timeout: float = 90.0) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/api/kernels",
            data=json.dumps({"name": kernel_name}).encode("utf-8"),
            headers=self._build_headers(),
            method="POST",
        )
        with self.opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def delete_kernel(self, kernel_id: str, timeout: float = 60.0) -> None:
        req = urllib.request.Request(
            f"{self.base_url}/api/kernels/{kernel_id}",
            headers=self._build_headers(),
            method="DELETE",
        )
        with self.opener.open(req, timeout=timeout) as resp:
            resp.read()

    async def connect_websocket(self, kernel_id: str) -> tornado.websocket.WebSocketClientConnection:
        ws_url = f"{self.ws_base_url}/api/kernels/{kernel_id}/channels"
        if self.token:
            ws_url += f"?token={self.token}"
        headers: dict[str, str] = {}
        cookie_header = self._build_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        req = tornado.httpclient.HTTPRequest(ws_url, headers=headers)
        return await tornado.websocket.websocket_connect(req)

    async def execute_code(
        self,
        conn: tornado.websocket.WebSocketClientConnection,
        code: str,
        timeout: float = 30.0,
    ) -> tuple[str, dict[str, Any]]:
        msg_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        request_msg = {
            "header": {
                "msg_id": msg_id,
                "username": "freebsd",
                "session": session_id,
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
            },
            "channel": "shell",
        }
        await conn.write_message(json.dumps(request_msg))

        stdout_chunks: list[str] = []
        reply_content: dict[str, Any] = {}
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(conn.read_message(), timeout=max(1.0, deadline - time.monotonic()))
            if raw is None:
                break
            msg = json.loads(raw)
            msg_type = msg.get("msg_type")
            content = msg.get("content", {})
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if msg_type == "stream" and content.get("name") == "stdout":
                stdout_chunks.append(content.get("text", ""))
            elif msg_type == "execute_reply":
                reply_content = content
                break

        return "".join(stdout_chunks), reply_content


class BenchmarkRunner:
    """Executes benchmark iterations across configured runtimes and modes."""

    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.results: list[IterationResult] = []
        self._active_kernels: set[str] = set()
        self._active_daemon_runtimes: set[str] = set()
        self._jupyter_helper: JupyterClientHelper | None = None
        self._daemon_client: RuntimeClient | None = None
        self._temp_key_path: str | None = None
        self._ssh_public_key: str | None = None
        self._interrupted = False

    def _setup_signal_handlers(self) -> None:
        def handler(sig: int, frame: Any) -> None:
            self._interrupted = True
            print("\n[!] Benchmark interrupted by user. Cleaning up active runtimes...", file=sys.stderr)
            self._emergency_cleanup()
            sys.exit(130)

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, AttributeError):
            pass

    def _emergency_cleanup(self) -> None:
        if self._jupyter_helper:
            for kid in list(self._active_kernels):
                try:
                    self._jupyter_helper.delete_kernel(kid, timeout=10.0)
                except Exception:
                    pass
        if self._daemon_client:
            for name in list(self._active_daemon_runtimes):
                try:
                    self._daemon_client.destroy(name)
                except Exception:
                    pass
        self._cleanup_temp_ssh_key()

    def _get_ssh_key(self) -> str:
        if self._ssh_public_key is not None:
            return self._ssh_public_key
        fd, path = tempfile.mkstemp(prefix="bench_ed25519_")
        os.close(fd)
        os.unlink(path)
        pub_path = f"{path}.pub"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path],
            check=True,
            capture_output=True,
        )
        self._temp_key_path = path
        with open(pub_path, encoding="utf-8") as f:
            self._ssh_public_key = f.read().strip()
        return self._ssh_public_key

    def _cleanup_temp_ssh_key(self) -> None:
        if self._temp_key_path:
            try:
                os.unlink(self._temp_key_path)
            except OSError:
                pass
            try:
                os.unlink(f"{self._temp_key_path}.pub")
            except OSError:
                pass
            self._temp_key_path = None
            self._ssh_public_key = None

    def _log(self, message: str) -> None:
        if not self.config.quiet:
            print(message, flush=True)

    def _log_verbose(self, message: str) -> None:
        if self.config.verbose and not self.config.quiet:
            print(f"  [debug] {message}", flush=True)

    async def benchmark_jupyter_runtime(
        self,
        runtime_key: str,
        spec: dict[str, Any],
        iteration: int,
        is_warmup: bool,
    ) -> IterationResult:
        assert self._jupyter_helper is not None
        kernel_spec_name = spec.get("name", runtime_key)
        display_name = spec.get("display_name", runtime_key)

        tag = f"[{display_name}] (iteration {iteration + 1}{' WARMUP' if is_warmup else ''})"
        self._log(f"\n>> {tag}: Initializing Jupyter launch...")

        result = IterationResult(
            runtime_alias=runtime_key,
            runtime_display=display_name,
            iteration=iteration + 1,
            is_warmup=is_warmup,
            mode="jupyter",
        )

        kernel_id: str | None = None
        conn: tornado.websocket.WebSocketClientConnection | None = None

        t0 = time.perf_counter()
        try:
            # 1. POST /api/kernels (Create kernel)
            t_create_start = time.perf_counter()
            resp = self._jupyter_helper.create_kernel(kernel_spec_name, timeout=self.config.timeout)
            t_create_end = time.perf_counter()
            result.create_sec = round(t_create_end - t_create_start, 4)
            kernel_id = resp.get("id")
            result.kernel_id = kernel_id
            if kernel_id:
                self._active_kernels.add(kernel_id)
            self._log_verbose(f"POST /api/kernels took {result.create_sec:.3f}s, kernel_id={kernel_id}")

            # 2. WebSocket Connect
            t_ws_start = time.perf_counter()
            conn = await self._jupyter_helper.connect_websocket(kernel_id)
            t_ws_end = time.perf_counter()
            result.ws_connect_sec = round(t_ws_end - t_ws_start, 4)
            self._log_verbose(f"WebSocket connect took {result.ws_connect_sec:.3f}s")

            # 3. First execution probe
            t_exec_start = time.perf_counter()
            probe_code = (
                "import json, sys, os, platform\n"
                "info = {'platform': sys.platform, 'nodename': os.uname().nodename, "
                "'system': platform.system(), 'release': platform.release(), 'python': sys.version.split()[0]}\n"
                "print('PROBE_DATA:' + json.dumps(info))\n"
            )
            stdout, reply = await self._jupyter_helper.execute_code(conn, probe_code, timeout=30.0)
            t_exec_end = time.perf_counter()
            result.first_exec_sec = round(t_exec_end - t_exec_start, 4)
            result.spinup_sec = round(t_exec_end - t0, 4)

            # Parse metadata
            for line in stdout.splitlines():
                if "PROBE_DATA:" in line:
                    raw_json = line.split("PROBE_DATA:", 1)[1].strip()
                    try:
                        result.guest_metadata = json.loads(raw_json)
                    except json.JSONDecodeError:
                        pass
                    break

            self._log(
                f"   [+] Spin-up ready in {result.spinup_sec:.3f}s "
                f"(create: {result.create_sec:.3f}s, ws: {result.ws_connect_sec:.3f}s, probe: {result.first_exec_sec:.3f}s)"
            )
            if result.guest_metadata:
                m = result.guest_metadata
                self._log_verbose(f"Guest environment: {m.get('system')} {m.get('release')} | Node: {m.get('nodename')} | Python: {m.get('python')}")

            # 4. Subsequent warm execution test
            t_warm_start = time.perf_counter()
            warm_stdout, warm_reply = await self._jupyter_helper.execute_code(conn, "1 + 1", timeout=10.0)
            t_warm_end = time.perf_counter()
            result.warm_exec_sec = round(t_warm_end - t_warm_start, 4)
            self._log_verbose(f"Subsequent warm execution took {result.warm_exec_sec:.3f}s")

        except Exception as error:
            result.success = False
            result.error = str(error)
            self._log(f"   [!] Error during spin-up: {error}")
        finally:
            if conn:
                try:
                    res = conn.close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            if kernel_id:
                t_del_start = time.perf_counter()
                try:
                    self._jupyter_helper.delete_kernel(kernel_id, timeout=self.config.timeout)
                    t_del_end = time.perf_counter()
                    result.destroy_sec = round(t_del_end - t_del_start, 4)
                    result.lifecycle_sec = round(time.perf_counter() - t0, 4)
                    self._log(
                        f"   [-] Destroyed in {result.destroy_sec:.3f}s "
                        f"(Full lifecycle: {result.lifecycle_sec:.3f}s)"
                    )
                except Exception as error:
                    self._log(f"   [!] Error during kernel destroy: {error}")
                    if result.success:
                        result.success = False
                        result.error = f"Destroy error: {error}"
                finally:
                    self._active_kernels.discard(kernel_id)

        return result

    def benchmark_daemon_runtime(
        self,
        runtime_key: str,
        spec: dict[str, Any],
        iteration: int,
        is_warmup: bool,
    ) -> IterationResult:
        assert self._daemon_client is not None
        display_name = spec.get("display_name", runtime_key)
        runtime_type = spec.get("type", "jail")
        profile = spec.get("profile")

        tag = f"[{display_name} (Direct Daemon)] (iteration {iteration + 1}{' WARMUP' if is_warmup else ''})"
        self._log(f"\n>> {tag}: Requesting direct runtime daemon provisioning...")

        result = IterationResult(
            runtime_alias=runtime_key,
            runtime_display=f"{display_name} (Daemon)",
            iteration=iteration + 1,
            is_warmup=is_warmup,
            mode="daemon",
        )

        name = f"freebsd-lab-b{int(time.time() * 1000) % 100000000:08d}"
        pubkey = self._get_ssh_key()
        pid = os.getpid()

        t0 = time.perf_counter()
        try:
            # 1. Socket Create
            t_create_start = time.perf_counter()
            if runtime_type == "jail":
                res = self._daemon_client.create_jail(name, pid, pubkey)
            else:
                res = self._daemon_client.create_bhyve(name, pid, pubkey, profile=profile or "freebsd-python")
            t_create_end = time.perf_counter()
            result.create_sec = round(t_create_end - t_create_start, 4)
            result.spinup_sec = result.create_sec
            self._active_daemon_runtimes.add(name)
            self._log(f"   [+] Direct daemon created in {result.create_sec:.3f}s (IP: {res.get('guest_ip')})")

        except Exception as error:
            result.success = False
            result.error = str(error)
            self._log(f"   [!] Error during direct daemon create: {error}")
        finally:
            if name in self._active_daemon_runtimes:
                t_del_start = time.perf_counter()
                try:
                    self._daemon_client.destroy(name)
                    t_del_end = time.perf_counter()
                    result.destroy_sec = round(t_del_end - t_del_start, 4)
                    result.lifecycle_sec = round(time.perf_counter() - t0, 4)
                    self._log(f"   [-] Direct daemon destroyed in {result.destroy_sec:.3f}s (Total: {result.lifecycle_sec:.3f}s)")
                except Exception as error:
                    self._log(f"   [!] Error during direct daemon destroy: {error}")
                    if result.success:
                        result.success = False
                        result.error = f"Destroy error: {error}"
                finally:
                    self._active_daemon_runtimes.discard(name)

        return result

    async def run(self) -> list[IterationResult]:
        self._setup_signal_handlers()

        # Initialize clients
        if self.config.mode in ("jupyter", "both"):
            self._jupyter_helper = JupyterClientHelper(
                base_url=self.config.base_url,
                token=self.config.token,
            )
            # Pre-flight check
            try:
                status = self._jupyter_helper.ping()
                self._log(f"Connected to JupyterLab at {self.config.base_url} (kernels active: {status.get('kernels', 0)})")
            except Exception as e:
                self._log(f"[!] Warning: Could not connect to JupyterLab at {self.config.base_url}: {e}")
                if self.config.mode == "jupyter":
                    raise

        if self.config.mode in ("daemon", "both"):
            self._daemon_client = RuntimeClient(
                socket_path=self.config.socket_path,
                timeout=self.config.timeout,
            )
            try:
                ping_info = self._daemon_client.ping()
                caps = ping_info.get("capabilities", [])
                self._log(f"Connected to Runtime Daemon at {self.config.socket_path} (capabilities: {', '.join(caps)})")
            except Exception as e:
                self._log(f"[!] Warning: Could not connect to Runtime Daemon at {self.config.socket_path}: {e}")
                if self.config.mode == "daemon":
                    raise

        # Resolve runtimes to benchmark
        selected_runtimes: list[tuple[str, dict[str, Any]]] = []
        for r_name in self.config.runtimes:
            clean = r_name.strip().lower()
            if clean in RUNTIME_PRESETS:
                selected_runtimes.append((clean, RUNTIME_PRESETS[clean]))
            else:
                # Custom kernelspec
                selected_runtimes.append(
                    (
                        clean,
                        {
                            "alias": clean,
                            "name": clean,
                            "display_name": clean,
                            "type": "jail" if "jail" in clean else "bhyve",
                            "profile": "freebsd-python" if "freebsd" in clean else "linux-python",
                        },
                    )
                )

        total_iterations = self.config.iterations + self.config.warmup
        self._log(
            f"\n=======================================================\n"
            f"  FreeBSD Laboratory Runtime Benchmark Suite\n"
            f"  Runtimes: {', '.join(k for k, _ in selected_runtimes)}\n"
            f"  Iterations: {self.config.iterations} (+ {self.config.warmup} warmup)\n"
            f"  Mode: {self.config.mode}\n"
            f"======================================================="
        )

        for i in range(total_iterations):
            is_warmup = i < self.config.warmup
            for r_key, r_spec in selected_runtimes:
                if self._interrupted:
                    break

                if self.config.mode in ("jupyter", "both"):
                    res = await self.benchmark_jupyter_runtime(
                        r_key, r_spec, iteration=i, is_warmup=is_warmup
                    )
                    self.results.append(res)

                if self.config.mode in ("daemon", "both"):
                    res = self.benchmark_daemon_runtime(
                        r_key, r_spec, iteration=i, is_warmup=is_warmup
                    )
                    self.results.append(res)

        self._cleanup_temp_ssh_key()
        return self.results


# ---------------------------------------------------------------------------
# Formatters & Report Generation
# ---------------------------------------------------------------------------


def format_table(summaries: list[RuntimeBenchmarkSummary], results: list[IterationResult]) -> str:
    out = io.StringIO()
    out.write("\n" + "=" * 100 + "\n")
    out.write(f"{'FREEBSD LABORATORY RUNTIME BENCHMARK RESULTS':^100}\n")
    out.write("=" * 100 + "\n\n")

    # Group by mode
    modes = sorted(list({s.mode for s in summaries}))
    for mode in modes:
        mode_label = "JUPYTER END-TO-END KERNEL RUNTIMES" if mode == "jupyter" else "DIRECT RUNTIME DAEMON (HYPERVISOR/ZFS)"
        out.write(f"--- {mode_label} ---\n\n")

        headers = ["Runtime", "Phase Metric", "Runs", "Mean", "Median", "Min", "Max", "StdDev", "P95"]
        col_widths = [24, 22, 6, 9, 9, 9, 9, 9, 9]

        header_str = " | ".join(f"{h:<{col_widths[idx]}}" for idx, h in enumerate(headers))
        separator_str = "-+-".join("-" * col_widths[idx] for idx in range(len(headers)))

        out.write(header_str + "\n")
        out.write(separator_str + "\n")

        phase_labels = [
            ("create_sec", "Create (POST/alloc)"),
            ("ws_connect_sec", "WebSocket Connect"),
            ("first_exec_sec", "First Code Probe"),
            ("spinup_sec", "Total Spin-Up"),
            ("warm_exec_sec", "Warm Code Exec"),
            ("destroy_sec", "Destroy (DELETE)"),
            ("lifecycle_sec", "Full Lifecycle"),
        ]

        mode_summaries = [s for s in summaries if s.mode == mode]
        for s in mode_summaries:
            first_row = True
            runs_str = f"{s.successful_runs}/{s.total_runs}"
            for metric_key, metric_label in phase_labels:
                if metric_key not in s.metrics:
                    continue
                m = s.metrics[metric_key]
                runtime_col = s.runtime_display if first_row else ""
                row = [
                    f"{runtime_col:<{col_widths[0]}}",
                    f"{metric_label:<{col_widths[1]}}",
                    f"{runs_str if first_row else '':<{col_widths[2]}}",
                    f"{m.mean:.3f}s" if m.count else "-",
                    f"{m.median:.3f}s" if m.count else "-",
                    f"{m.min:.3f}s" if m.count else "-",
                    f"{m.max:.3f}s" if m.count else "-",
                    f"{m.std_dev:.3f}s" if m.count else "-",
                    f"{m.p95:.3f}s" if m.count else "-",
                ]
                formatted_row = " | ".join(f"{val:<{col_widths[idx]}}" for idx, val in enumerate(row))
                out.write(formatted_row + "\n")
                first_row = False
            out.write(separator_str + "\n")
        out.write("\n")

    # Guest Environment Introspection
    out.write("--- GUEST ENVIRONMENT INTROSPECTION ---\n\n")
    guest_headers = ["Runtime", "OS Platform", "Kernel Release", "Python Version", "Probe Node / Host"]
    guest_widths = [26, 16, 20, 16, 26]
    g_header = " | ".join(f"{h:<{guest_widths[idx]}}" for idx, h in enumerate(guest_headers))
    g_sep = "-+-".join("-" * guest_widths[idx] for idx in range(len(guest_headers)))
    out.write(g_header + "\n")
    out.write(g_sep + "\n")

    for s in summaries:
        if s.guest_metadata:
            meta = s.guest_metadata
            row = [
                s.runtime_display,
                f"{meta.get('system', '')} ({meta.get('platform', '')})",
                meta.get("release", ""),
                meta.get("python", ""),
                meta.get("nodename", ""),
            ]
            out.write(" | ".join(f"{val:<{guest_widths[idx]}}" for idx, val in enumerate(row)) + "\n")
    out.write(g_sep + "\n\n")

    return out.getvalue()


def format_markdown(summaries: list[RuntimeBenchmarkSummary], results: list[IterationResult]) -> str:
    out = io.StringIO()
    out.write("# FreeBSD Laboratory Runtime Benchmark Report\n\n")

    modes = sorted(list({s.mode for s in summaries}))
    for mode in modes:
        mode_label = "Jupyter End-to-End Kernel Runtimes" if mode == "jupyter" else "Direct Runtime Daemon"
        out.write(f"## {mode_label}\n\n")
        out.write("| Runtime | Metric Phase | Runs | Mean | Median | Min | Max | Std Dev | P95 |\n")
        out.write("|---|---|---|---|---|---|---|---|---|\n")

        phase_labels = [
            ("create_sec", "Create / Provision"),
            ("ws_connect_sec", "WebSocket Connect"),
            ("first_exec_sec", "First Code Probe"),
            ("spinup_sec", "**Total Spin-Up**"),
            ("warm_exec_sec", "Warm Execution"),
            ("destroy_sec", "**Destroy / Cleanup**"),
            ("lifecycle_sec", "**Full Lifecycle**"),
        ]

        mode_summaries = [s for s in summaries if s.mode == mode]
        for s in mode_summaries:
            first_row = True
            runs_str = f"{s.successful_runs}/{s.total_runs}"
            for metric_key, metric_label in phase_labels:
                if metric_key not in s.metrics:
                    continue
                m = s.metrics[metric_key]
                runtime_col = f"**{s.runtime_display}**" if first_row else ""
                out.write(
                    f"| {runtime_col} | {metric_label} | {runs_str if first_row else ''} | "
                    f"`{m.mean:.3f}s` | `{m.median:.3f}s` | `{m.min:.3f}s` | `{m.max:.3f}s` | "
                    f"`{m.std_dev:.3f}s` | `{m.p95:.3f}s` |\n"
                )
                first_row = False
        out.write("\n")

    # Guest details table
    out.write("## Guest Runtime Environment Details\n\n")
    out.write("| Runtime | OS Platform | Kernel Release | Python Version | Hostname |\n")
    out.write("|---|---|---|---|---|\n")
    for s in summaries:
        if s.guest_metadata:
            meta = s.guest_metadata
            out.write(
                f"| **{s.runtime_display}** | {meta.get('system', '')} ({meta.get('platform', '')}) | "
                f"`{meta.get('release', '')}` | `{meta.get('python', '')}` | `{meta.get('nodename', '')}` |\n"
            )
    out.write("\n")

    return out.getvalue()


def format_json_data(summaries: list[RuntimeBenchmarkSummary], results: list[IterationResult], config: BenchmarkConfig) -> str:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": asdict(config),
        "host_platform": platform.platform(),
        "summaries": [asdict(s) for s in summaries],
        "iterations": [asdict(r) for r in results],
    }
    return json.dumps(payload, indent=2)


def format_csv_data(summaries: list[RuntimeBenchmarkSummary], results: list[IterationResult]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "runtime_alias",
        "runtime_display",
        "mode",
        "iteration",
        "is_warmup",
        "success",
        "create_sec",
        "ws_connect_sec",
        "first_exec_sec",
        "spinup_sec",
        "warm_exec_sec",
        "destroy_sec",
        "lifecycle_sec",
        "error",
        "guest_os",
        "guest_release",
        "guest_python",
    ])
    for r in results:
        meta = r.guest_metadata or {}
        writer.writerow([
            r.runtime_alias,
            r.runtime_display,
            r.mode,
            r.iteration,
            r.is_warmup,
            r.success,
            r.create_sec,
            r.ws_connect_sec,
            r.first_exec_sec,
            r.spinup_sec,
            r.warm_exec_sec,
            r.destroy_sec,
            r.lifecycle_sec,
            r.error or "",
            meta.get("system", ""),
            meta.get("release", ""),
            meta.get("python", ""),
        ])
    return out.getvalue()


def write_output_file(path_str: str, content: str) -> None:
    raw_path = Path(path_str).expanduser()
    if raw_path.is_symlink():
        raise RuntimeError(f"Refusing to write to symlinked destination: {raw_path}")
    parent_dir = raw_path.parent
    if parent_dir.is_symlink():
        raise RuntimeError(f"Refusing to write to symlinked parent directory: {parent_dir}")
    parent_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{raw_path.name}.",
        dir=str(parent_dir),
        text=True,
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o644, follow_symlinks=False)
        tmp_path.replace(raw_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark FreeBSD Jails, FreeBSD bhyve VMs, and Linux bhyve VMs spin up and destroy times."
    )
    parser.add_argument(
        "--runtimes",
        "-r",
        default="all",
        help="Comma-separated runtimes to benchmark: 'all', 'jail', 'freebsd-bhyve', 'linux-bhyve' (default: all)",
    )
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=3,
        help="Number of benchmark iterations per runtime (default: 3)",
    )
    parser.add_argument(
        "--warmup",
        "-w",
        type=int,
        default=0,
        help="Number of warmup iterations to run before recording stats (default: 0)",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["jupyter", "daemon", "both"],
        default="jupyter",
        help="Benchmarking mode: 'jupyter' (end-to-end), 'daemon' (socket only), 'both' (default: jupyter)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("JUPYTER_BASE_URL", "http://127.0.0.1:8888"),
        help="Jupyter server base URL (default: http://127.0.0.1:8888)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("JUPYTER_TOKEN", ""),
        help="Jupyter authentication token (default: auto-detected from session)",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_RUNTIME_SOCKET,
        help=f"Runtime daemon socket path (default: {DEFAULT_RUNTIME_SOCKET})",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "markdown", "json", "csv"],
        default="table",
        help="Output report format (default: table)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Path to save benchmark report file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Timeout in seconds for runtime operations (default: 90.0)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress live progress messages and print only final report",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print verbose debug logs during benchmark execution",
    )
    return parser


def parse_runtimes_arg(arg: str) -> list[str]:
    if not arg or arg.lower() == "all":
        return ["jail", "freebsd-bhyve", "linux-bhyve"]
    return [item.strip() for item in arg.split(",") if item.strip()]


async def async_main(args: argparse.Namespace) -> int:
    runtimes = parse_runtimes_arg(args.runtimes)
    config = BenchmarkConfig(
        mode=args.mode,
        runtimes=runtimes,
        iterations=max(1, args.iterations),
        warmup=max(0, args.warmup),
        base_url=args.base_url,
        token=args.token,
        socket_path=args.socket,
        timeout=args.timeout,
        output_format=args.format,
        output_file=args.output,
        verbose=args.verbose,
        quiet=args.quiet,
    )

    runner = BenchmarkRunner(config)
    results = await runner.run()
    summaries = summarize_results(results)

    # Format output
    if config.output_format == "markdown":
        report = format_markdown(summaries, results)
    elif config.output_format == "json":
        report = format_json_data(summaries, results, config)
    elif config.output_format == "csv":
        report = format_csv_data(summaries, results)
    else:
        report = format_table(summaries, results)

    # Output to stdout
    print(report)

    # Save to file if requested
    if config.output_file:
        write_output_file(config.output_file, report)
        if not config.quiet:
            print(f"\n[+] Benchmark report saved to: {config.output_file}")

    # Return exit code based on failures
    has_failures = any(not r.success for r in results if not r.is_warmup)
    return 1 if has_failures else 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(async_main(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
