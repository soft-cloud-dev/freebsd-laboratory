from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from freebsd_laboratory.benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    IterationResult,
    JupyterClientHelper,
    compute_metric_summary,
    format_csv_data,
    format_json_data,
    format_markdown,
    format_table,
    parse_runtimes_arg,
    summarize_results,
    write_output_file,
)


def test_compute_metric_summary_empty() -> None:
    summary = compute_metric_summary([])
    assert summary.count == 0
    assert summary.mean == 0.0
    assert summary.min == 0.0
    assert summary.max == 0.0


def test_compute_metric_summary_single() -> None:
    summary = compute_metric_summary([4.5])
    assert summary.count == 1
    assert summary.mean == 4.5
    assert summary.median == 4.5
    assert summary.min == 4.5
    assert summary.max == 4.5
    assert summary.std_dev == 0.0
    assert summary.p95 == 4.5


def test_compute_metric_summary_multiple() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    summary = compute_metric_summary(values)
    assert summary.count == 5
    assert summary.mean == 3.0
    assert summary.median == 3.0
    assert summary.min == 1.0
    assert summary.max == 5.0
    assert summary.std_dev == pytest.approx(1.5811, abs=1e-3)
    assert summary.p95 == pytest.approx(4.8, abs=1e-2)


def test_parse_runtimes_arg() -> None:
    assert parse_runtimes_arg("all") == ["jail", "freebsd-bhyve", "linux-bhyve"]
    assert parse_runtimes_arg("") == ["jail", "freebsd-bhyve", "linux-bhyve"]
    assert parse_runtimes_arg("jail,linux-bhyve") == ["jail", "linux-bhyve"]
    assert parse_runtimes_arg("freebsd-bhyve") == ["freebsd-bhyve"]


def test_summarize_results_discards_warmup_and_computes_stats() -> None:
    results = [
        IterationResult(
            runtime_alias="jail",
            runtime_display="FreeBSD VNET Jail",
            iteration=1,
            is_warmup=True,
            mode="jupyter",
            create_sec=9.99,
            destroy_sec=9.99,
            lifecycle_sec=19.98,
            spinup_sec=10.0,
            success=True,
        ),
        IterationResult(
            runtime_alias="jail",
            runtime_display="FreeBSD VNET Jail",
            iteration=2,
            is_warmup=False,
            mode="jupyter",
            create_sec=1.5,
            ws_connect_sec=0.5,
            first_exec_sec=0.2,
            spinup_sec=2.2,
            warm_exec_sec=0.05,
            destroy_sec=1.0,
            lifecycle_sec=3.25,
            success=True,
            guest_metadata={"system": "FreeBSD", "release": "15.1-RELEASE"},
        ),
        IterationResult(
            runtime_alias="jail",
            runtime_display="FreeBSD VNET Jail",
            iteration=3,
            is_warmup=False,
            mode="jupyter",
            create_sec=1.7,
            ws_connect_sec=0.6,
            first_exec_sec=0.3,
            spinup_sec=2.6,
            warm_exec_sec=0.06,
            destroy_sec=1.2,
            lifecycle_sec=3.86,
            success=True,
            guest_metadata={"system": "FreeBSD", "release": "15.1-RELEASE"},
        ),
    ]

    summaries = summarize_results(results)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.runtime_alias == "jail"
    assert s.total_runs == 2
    assert s.successful_runs == 2
    assert s.failed_runs == 0
    assert s.guest_metadata["system"] == "FreeBSD"

    assert s.metrics["create_sec"].count == 2
    assert s.metrics["create_sec"].mean == 1.6
    assert s.metrics["create_sec"].min == 1.5
    assert s.metrics["create_sec"].max == 1.7
    assert s.metrics["spinup_sec"].mean == 2.4
    assert s.metrics["destroy_sec"].mean == 1.1


def test_format_table_and_markdown() -> None:
    results = [
        IterationResult(
            runtime_alias="jail",
            runtime_display="FreeBSD VNET Jail",
            iteration=1,
            is_warmup=False,
            mode="jupyter",
            create_sec=1.5,
            ws_connect_sec=0.5,
            first_exec_sec=0.2,
            spinup_sec=2.2,
            warm_exec_sec=0.05,
            destroy_sec=1.0,
            lifecycle_sec=3.25,
            success=True,
            guest_metadata={"system": "FreeBSD", "release": "15.1-RELEASE", "nodename": "test-node", "python": "3.12.13"},
        ),
        IterationResult(
            runtime_alias="linux-bhyve",
            runtime_display="Linux bhyve VM",
            iteration=1,
            is_warmup=False,
            mode="jupyter",
            create_sec=6.5,
            ws_connect_sec=0.8,
            first_exec_sec=0.1,
            spinup_sec=7.4,
            warm_exec_sec=0.04,
            destroy_sec=1.1,
            lifecycle_sec=8.54,
            success=True,
            guest_metadata={"system": "Linux", "release": "6.6.78", "nodename": "test-linux", "python": "3.12.13"},
        ),
    ]
    summaries = summarize_results(results)

    table = format_table(summaries, results)
    assert "FREEBSD LABORATORY RUNTIME BENCHMARK RESULTS" in table
    assert "FreeBSD VNET Jail" in table
    assert "Linux bhyve VM" in table
    assert "GUEST ENVIRONMENT INTROSPECTION" in table

    md = format_markdown(summaries, results)
    assert "# FreeBSD Laboratory Runtime Benchmark Report" in md
    assert "FreeBSD VNET Jail" in md
    assert "Linux bhyve VM" in md


def test_format_json_and_csv() -> None:
    config = BenchmarkConfig(iterations=2, mode="jupyter")
    results = [
        IterationResult(
            runtime_alias="jail",
            runtime_display="FreeBSD VNET Jail",
            iteration=1,
            is_warmup=False,
            mode="jupyter",
            create_sec=1.5,
            destroy_sec=1.0,
            lifecycle_sec=2.5,
            success=True,
            guest_metadata={"system": "FreeBSD", "release": "15.1", "python": "3.12"},
        )
    ]
    summaries = summarize_results(results)

    json_str = format_json_data(summaries, results, config)
    parsed = json.loads(json_str)
    assert "summaries" in parsed
    assert "iterations" in parsed
    assert parsed["config"]["iterations"] == 2

    csv_str = format_csv_data(summaries, results)
    reader = list(csv.reader(csv_str.splitlines()))
    assert reader[0][0] == "runtime_alias"
    assert reader[1][0] == "jail"
    assert reader[1][1] == "FreeBSD VNET Jail"


def test_write_output_file_security(tmp_path: Path) -> None:
    out_file = tmp_path / "report.md"
    write_output_file(str(out_file), "# Benchmark")
    assert out_file.read_text(encoding="utf-8") == "# Benchmark"

    # Anti-symlink check: refuse symlinked target
    symlink_file = tmp_path / "symlink_report.md"
    symlink_file.symlink_to(out_file)
    with pytest.raises(RuntimeError, match="Refusing to write to symlinked destination"):
        write_output_file(str(symlink_file), "overwrite attempt")


def test_jupyter_client_helper_mock() -> None:
    helper = JupyterClientHelper(base_url="http://127.0.0.1:8888", token="test-token")
    assert helper.token == "test-token"
    assert helper.ws_base_url == "ws://127.0.0.1:8888"

    headers = helper._build_headers()
    assert headers["Authorization"] == "token test-token"


def test_benchmark_runner_mock_jupyter(monkeypatch: pytest.MonkeyPatch) -> None:
    config = BenchmarkConfig(
        mode="jupyter",
        runtimes=["jail"],
        iterations=1,
        warmup=0,
    )
    runner = BenchmarkRunner(config)

    fake_helper = MagicMock()
    fake_helper.ping.return_value = {"kernels": 0}
    fake_helper.create_kernel.return_value = {"id": "fake-kernel-123"}
    fake_helper.delete_kernel.return_value = None

    fake_ws = AsyncMock()
    fake_helper.connect_websocket = AsyncMock(return_value=fake_ws)
    fake_helper.execute_code = AsyncMock(
        side_effect=[
            ("PROBE_DATA:{\"system\": \"FreeBSD\", \"release\": \"15.1\", \"python\": \"3.12\"}\n", {}),
            ("", {}),
        ]
    )

    monkeypatch.setattr(runner, "_jupyter_helper", fake_helper)
    monkeypatch.setattr("freebsd_laboratory.benchmark.JupyterClientHelper", lambda **kw: fake_helper)

    results = asyncio.run(runner.run())
    assert len(results) == 1
    r = results[0]
    assert r.success is True
    assert r.runtime_alias == "jail"
    assert r.kernel_id == "fake-kernel-123"
    assert r.guest_metadata.get("system") == "FreeBSD"
    assert r.create_sec >= 0.0
    assert r.destroy_sec >= 0.0


def test_benchmark_runner_mock_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    config = BenchmarkConfig(
        mode="daemon",
        runtimes=["jail", "linux-bhyve"],
        iterations=1,
        warmup=0,
    )
    runner = BenchmarkRunner(config)

    fake_client = MagicMock()
    fake_client.ping.return_value = {"capabilities": ["jail", "bhyve.linux"]}
    fake_client.create_jail.return_value = {"guest_ip": "172.31.254.10", "type": "jail"}
    fake_client.create_bhyve.return_value = {"guest_ip": "172.31.254.11", "type": "bhyve"}
    fake_client.destroy.return_value = {"removed": ["jail"]}

    monkeypatch.setattr(runner, "_get_ssh_key", lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...")
    monkeypatch.setattr("freebsd_laboratory.benchmark.RuntimeClient", lambda **kw: fake_client)

    results = asyncio.run(runner.run())
    assert len(results) == 2
    assert results[0].success is True
    assert results[0].runtime_alias == "jail"
    assert results[1].success is True
    assert results[1].runtime_alias == "linux-bhyve"
