"""一键演示脚本。

启动服务、入队 fixture 采集、上传示例文档、查询；适合面试录屏与
冒烟测试。
"""

import argparse
import io
import sys
import time
from pathlib import Path

# 把项目根目录加入 sys.path，保证 `python scripts/demo.py` 也能 import app
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import requests  # noqa: E402  必须放在 sys.path 调整之后

from app.cli import cmd_seed_docs  # noqa: E402
from app.background import wait_for_jobs  # noqa: E402
from app.db import init_db  # noqa: E402


def wait_for(base_url: str, path: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}{path}", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.2)
    raise RuntimeError(f"服务未就绪: {last_err}")


def _safe_json(resp, label: str) -> dict:
    """把 requests 响应解析为 dict；失败时打印原始 body 而不是抛 JSONDecodeError。"""
    try:
        return resp.json()
    except ValueError:
        body_preview = (resp.text or "")[:300].replace("\n", "\\n")
        print(
            f"  ! {label} 返回非 JSON: status={resp.status_code} body={body_preview!r}",
            file=sys.stderr,
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="电商工作台一键演示")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--no-server", action="store_true", help="跳过服务存活检查")
    args = parser.parse_args()

    print("[1/5] init-db")
    init_db()

    if not args.no_server:
        print(f"[2/5] 检查服务 {args.base_url}")
        wait_for(args.base_url, "/health")
        print(f"[3/5] 触发后台 fixture 采集")
        r = requests.post(f"{args.base_url}/api/crawl", timeout=15)
        if r.status_code >= 400:
            print(f"  ! /api/crawl 失败: {r.status_code} {r.text[:300]}", file=sys.stderr)
            return 1
        body = _safe_json(r, "/api/crawl")
        print(f"  -> status={r.status_code} job_id={body.get('id')}")
        wait_for_jobs(timeout=15)
    else:
        print("[2-3/5] 跳过服务相关步骤（--no-server）")

    print("[4/5] 注入示例文档并索引（PDF + DOCX）")
    cmd_seed_docs([])

    print("[5/5] 客服 RAG 查询")
    q = {"query": "green tea"}
    r = requests.post(f"{args.base_url}/api/rag/query", json=q, timeout=15)
    if r.status_code >= 400:
        print(f"  ! /api/rag/query 失败: {r.status_code} {r.text[:300]}", file=sys.stderr)
        return 1
    body = _safe_json(r, "/api/rag/query")
    print(f"  answer   : {body.get('answer')!r}")
    print(f"  no_answer: {body.get('no_answer')}")
    print(f"  hits     : {len(body.get('citations', []))}")

    print("演示完成。可访问 http://127.0.0.1:8000/api/settings 查看运行状态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())