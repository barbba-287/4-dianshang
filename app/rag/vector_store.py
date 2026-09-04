"""本地向量库与 cosine 相似度。

S3 阶段使用内存 + JSON 持久化；超过 10k 向量时建议迁移到 Qdrant
或 pgvector。接口 `VectorStore` 与生产实现一致，业务代码无需修改。
"""

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "VectorRecord":
        return cls(id=data["id"], vector=list(data["vector"]), payload=dict(data.get("payload", {})))


class VectorStore(Protocol):
    def add(self, records: Sequence[VectorRecord]) -> None: ...
    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        score_threshold: float,
        filter_payload: dict | None = None,
    ) -> list[tuple[VectorRecord, float]]: ...
    def delete_by_version(self, version_id: int) -> int: ...


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class InMemoryVectorStore:
    """启动时从 JSON 加载，写入时持久化；不引入额外依赖。"""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._records: dict[str, VectorRecord] = {}
        if self._path and self._path.exists():
            self._load()

    @property
    def records(self) -> list[VectorRecord]:
        return list(self._records.values())

    def _load(self) -> None:
        assert self._path is not None
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._records = {
            item["id"]: VectorRecord.from_json(item) for item in data.get("records", [])
        }

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"records": [r.to_json() for r in self._records.values()]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def add(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            self._records[record.id] = record
        self._flush()

    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        score_threshold: float,
        filter_payload: dict | None = None,
    ) -> list[tuple[VectorRecord, float]]:
        scored: list[tuple[VectorRecord, float]] = []
        for record in self._records.values():
            if filter_payload:
                matches = True
                for key, expected in filter_payload.items():
                    actual = record.payload.get(key)
                    # A document may be linked to more than one product.  Treat
                    # list-valued payload fields as a membership filter while
                    # retaining exact matching for scalar metadata.
                    if isinstance(actual, (list, tuple, set)):
                        if expected not in actual:
                            matches = False
                            break
                    elif actual != expected:
                        matches = False
                        break
                if not matches:
                    continue
            score = cosine_similarity(vector, record.vector)
            if score < score_threshold:
                continue
            scored.append((record, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

    def delete_by_version(self, version_id: int) -> int:
        removed = 0
        for key in list(self._records.keys()):
            payload = self._records[key].payload
            if payload.get("document_version_id") == version_id:
                del self._records[key]
                removed += 1
        if removed:
            self._flush()
        return removed

    def reset(self) -> None:
        self._records.clear()
        self._flush()