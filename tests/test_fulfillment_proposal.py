from io import BytesIO
from datetime import datetime
from decimal import Decimal

from openpyxl import load_workbook

from app.exports import fulfillment_proposal_xlsx


def test_fulfillment_proposal_workbook_has_disclaimer_and_detail_sheets():
    orders = [{
        "platform": "mock", "store_ref": "demo-store", "external_order_no": "ORD-1", "order_status": "paid",
        "external_created_at": datetime(2026, 10, 6, 9), "paid_at": datetime(2026, 10, 6, 9, 1),
        "gross_amount": Decimal("29.90"), "refund_amount": Decimal("0"), "currency": "CNY",
        "data_completeness": "complete", "source_mode": "mock", "simulated": True, "status_reason": None,
        "lines": [{"external_sku": "SKU-1", "internal_sku_id": None, "ordered_qty": 2, "cancelled_qty": 0, "refunded_qty": 0, "gross_amount": Decimal("29.90"), "currency": "CNY", "mapping_status": "unmapped", "data_completeness": "complete"}],
    }]
    content, meta = fulfillment_proposal_xlsx(orders, meta={"workspace_id": 1, "as_of": datetime(2026, 10, 6, 10), "source_mode": "mock", "simulated": True, "data_completeness": "complete"})
    book = load_workbook(BytesIO(content))
    assert book.sheetnames == ["待发订单清单", "待拣商品明细"]
    assert "说明" not in book.sheetnames
    assert [cell.value for cell in book["待发订单清单"][1]] == ["平台", "店铺", "订单号", "订单状态", "下单时间", "支付时间", "订单金额", "退款金额", "币种", "数据完整度", "来源", "人工核对"]
    assert [cell.value for cell in book["待拣商品明细"][1]] == ["平台", "店铺", "订单号", "商品 SKU", "内部商品", "规格", "待拣数量", "取消数量", "退款数量", "金额", "币种", "映射状态", "数据完整度", "人工核对"]
    assert book["待发订单清单"]["D2"].value == "已支付"
    assert book["待发订单清单"]["K2"].value == "模拟/回放"
    assert book["待拣商品明细"]["E2"].value == "未映射"
    assert book["待拣商品明细"]["F2"].value == "—"
    assert "需要建立商品映射" in str(book["待拣商品明细"]["N2"].value)
    assert book["待拣商品明细"]["G2"].value == 2
    assert book["待发订单清单"]["A1"].border.left.style == "thin"
    assert meta["order_count"] == 1
    assert meta["row_count"] == 1
