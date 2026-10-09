"""本地类目 HTML 回放采集器测试。"""

import json
from pathlib import Path

import pytest

from app.catalog_replay import CatalogReplayError, ReplayLimits, replay_catalog


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "catalog_replay"


def manifest_for(tmp_path: Path, entrypoint: str = "page-1.html") -> Path:
    root = tmp_path / "replay"
    root.mkdir()
    for name in ("page-1.html", "page-2.html", "blocked.html", "empty.html", "invalid-price.html"):
        source = FIXTURE_DIR / name
        if source.exists():
            (root / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"mode": "local_html_replay", "root": ".", "entrypoint": entrypoint}),
        encoding="utf-8",
    )
    return manifest


def test_replay_collects_and_deduplicates_pages(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    waits: list[float] = []
    result = replay_catalog(
        manifest_for(tmp_path),
        limits=ReplayLimits(max_pages=3, delay_seconds=0.25),
        artifacts_dir=artifacts,
        sleeper=waits.append,
    )

    assert result.pages_seen == 2
    assert result.data_completeness == "complete"
    assert [record.external_product_id for record in result.records] == [
        "competitor-001",
        "competitor-002",
        "competitor-003",
    ]
    assert waits == [0.25]
    run_path = artifacts / "catalog-replay" / result.run_id / "run.json"
    raw_path = artifacts / "catalog-replay" / result.run_id / "raw-products.json"
    assert run_path.is_file()
    assert raw_path.is_file()
    assert json.loads(run_path.read_text(encoding="utf-8"))["allow_network"] is False


def test_replay_rejects_blocked_page(tmp_path):
    manifest = manifest_for(tmp_path, "blocked.html")

    with pytest.raises(CatalogReplayError, match="登录、验证码") as error:
        replay_catalog(manifest, artifacts_dir=tmp_path / "artifacts")

    assert error.value.code == "BLOCKED_PAGE"


def test_replay_rejects_empty_page(tmp_path):
    with pytest.raises(CatalogReplayError) as error:
        replay_catalog(manifest_for(tmp_path, "empty.html"), artifacts_dir=tmp_path / "artifacts")
    assert error.value.code == "NO_PRODUCTS"


def test_replay_rejects_invalid_product(tmp_path):
    with pytest.raises(CatalogReplayError) as error:
        replay_catalog(manifest_for(tmp_path, "invalid-price.html"), artifacts_dir=tmp_path / "artifacts")
    assert error.value.code == "INVALID_PRODUCT"


def test_replay_rejects_network_manifest(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"mode": "local_html_replay", "root": ".", "entrypoint": "https://example.com"}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogReplayError) as error:
        replay_catalog(manifest, artifacts_dir=tmp_path / "artifacts")
    assert error.value.code == "NETWORK_DISABLED"


def test_replay_page_limit_is_partial(tmp_path):
    with pytest.raises(CatalogReplayError) as error:
        replay_catalog(
            manifest_for(tmp_path),
            limits=ReplayLimits(max_pages=1, delay_seconds=0),
            artifacts_dir=tmp_path / "artifacts",
        )
    assert error.value.code == "PAGE_LIMIT_EXCEEDED"
    assert error.value.data_completeness == "partial"


def test_replay_can_be_cancelled(tmp_path):
    with pytest.raises(CatalogReplayError) as error:
        replay_catalog(
            manifest_for(tmp_path),
            cancel_check=lambda: True,
            artifacts_dir=tmp_path / "artifacts",
        )
    assert error.value.code == "CANCELLED"
