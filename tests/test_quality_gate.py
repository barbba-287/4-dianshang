import io

from PIL import Image

from app.quality_gate import run_quality_gate


def test_quality_gate_blocks_invalid_image_and_claim():
    result = run_quality_gate(
        content={"title": "商品", "selling_points": ["保证第一"], "claims": []},
        snapshot={"current_price": "9.90"},
        images=[b"not-an-image"],
    )
    assert result.status == "blocked"
    assert {item.code for item in result.issues} >= {"IMAGE_INVALID", "FORBIDDEN_CLAIM"}


def test_quality_gate_passes_valid_candidate():
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, format="PNG")
    result = run_quality_gate(
        content={"title": "商品", "selling_points": ["清晰展示"], "claims": []},
        snapshot={"current_price": "9.90"},
        images=[buffer.getvalue()],
    )
    assert result.status == "warn"
