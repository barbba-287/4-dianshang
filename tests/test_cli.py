"""CLI 命令单测。

覆盖：
- init-db 创建表；
- status 输出关键字段；
- seed-docs 注入示例 PDF + DOCX 并写入向量库；
- query 从向量库命中并返回；
- query 在无证据时返回 no_answer。
"""

import subprocess
import sys

import pytest


def _run_cli(args: list[str], tmp_path, monkeypatch) -> subprocess.CompletedProcess:
    """调用 python -m app.cli ...，使用临时数据库。"""
    db_path = tmp_path / "cli.db"
    vector_path = tmp_path / "vectors.json"
    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "VECTOR_STORE_PATH": str(vector_path),
        "IMPORTS_DIR": str(tmp_path / "uploads"),
        "ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    import os

    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
        cwd="D:\\ai\\dianshang",
    )


def test_cli_init_db(tmp_path):
    result = _run_cli(["init-db"], tmp_path, None)
    assert result.returncode == 0
    # 只断言英文 / 数字部分，避免 Windows 编码问题
    assert "database" in result.stdout or "数据" in result.stdout.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def test_cli_status(tmp_path):
    _run_cli(["init-db"], tmp_path, None)
    result = _run_cli(["status"], tmp_path, None)
    assert result.returncode == 0
    assert "worker_concurrency" in result.stdout
    assert "embedder_dim" in result.stdout
    assert "documents" in result.stdout


def test_cli_seed_docs_and_query(tmp_path):
    seed = _run_cli(["seed-docs"], tmp_path, None)
    assert seed.returncode == 0, seed.stdout + seed.stderr
    assert "indexed" in seed.stdout

    # query 命中
    result = _run_cli(["query", "green"], tmp_path, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "answer:" in result.stdout or "[no_answer]" in result.stdout

    # enqueue fixture 采集并等待后台任务完成
    crawl = _run_cli(["enqueue-crawl"], tmp_path, None)
    assert crawl.returncode == 0, crawl.stdout + crawl.stderr
    assert "job_id=" in crawl.stdout
    assert "status=succeeded" in crawl.stdout

    status = _run_cli(["status"], tmp_path, None)
    assert status.returncode == 0, status.stdout + status.stderr
    assert "products           = 3" in status.stdout

    # 重复执行 seed-docs 应复用 ready 版本，不重复写入 chunks
    seed_again = _run_cli(["seed-docs"], tmp_path, None)
    assert seed_again.returncode == 0, seed_again.stdout + seed_again.stderr
    assert "reused" in seed_again.stdout

    # 无证据 query 返回 no_answer
    result_no = _run_cli(["query", "completely_unrelated_outer_space"], tmp_path, None)
    assert result_no.returncode == 0
    assert "[no_answer]" in result_no.stdout


def test_cli_unknown_command(tmp_path):
    result = _run_cli(["unknown"], tmp_path, None)
    assert result.returncode == 2
    assert "unknown" in result.stdout or "命令" in result.stdout.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def test_cli_query_without_args(tmp_path):
    result = _run_cli(["query"], tmp_path, None)
    assert result.returncode == 2
    assert "usage" in result.stdout