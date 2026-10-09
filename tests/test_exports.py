"""运营待办 CSV 导出单元测试。"""

import csv
import io

from app.exports import operations_todo_csv


def test_operations_todo_csv_has_bom_stable_columns_and_hash():
    content, meta = operations_todo_csv([{
        "todo_id": "alert:1",
        "type": "inventory_alert",
        "priority": "high",
        "status": "open",
        "title": "库存告警",
        "reason": "需要人工处理",
        "entity_type": "inventory_alert",
        "entity_id": 1,
        "warehouse_id": 2,
        "source": "InventoryAlert",
        "due_at": None,
        "allowed_actions": ["view", "acknowledge"],
        "target_path": "/dashboard#alerts-panel",
    }])

    assert content.startswith(b"\xef\xbb\xbf")
    assert meta["row_count"] == 1
    import hashlib
    assert meta["sha256"] == hashlib.sha256(content).hexdigest()
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0] == ["待办类型", "优先级", "状态", "待办事项", "原因", "仓库"]
    assert rows[1] == ["库存告警", "高", "待处理", "库存告警", "需要人工处理", "2"]


def test_operations_todo_csv_escapes_spreadsheet_formulas_and_empty_has_header():
    content, meta = operations_todo_csv([{
        "type": "+command",
        "priority": "-1",
        "status": "@mention",
        "title": "=HYPERLINK(\"https://bad.example\")",
        "reason": "安全文本",
    }])
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert all(rows[1][index].startswith("'") for index in range(3))
    assert rows[1][3].startswith("'")
    assert meta["row_count"] == 1

    empty, empty_meta = operations_todo_csv([])
    empty_rows = list(csv.reader(io.StringIO(empty.decode("utf-8-sig"))))
    assert len(empty_rows) == 1
    assert empty_meta["row_count"] == 0
