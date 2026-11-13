"""`QdrantVectorStore.drop()` 的安全护栏测试。

这条护栏的目的很具体：**基准/测试脚本不得删除业务集合**。
它不需要真实 Qdrant 服务 —— 护栏在调用客户端之前就会拦下来。
"""

from __future__ import annotations

import pytest

from aieb.brain.retrieval.vector_store import (
    DROPPABLE_PREFIXES,
    QdrantVectorStore,
    VectorStoreError,
)


class RecordingClient:
    """只记录 delete_collection 调用，不连网。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_collection(self, name: str) -> None:  # noqa: D401 - 桩
        self.deleted.append(name)


def make_store(collection: str) -> QdrantVectorStore:
    store = QdrantVectorStore(url="http://127.0.0.1:1", collection=collection, dim=8)
    store._client = RecordingClient()
    return store


@pytest.mark.parametrize(
    "collection",
    ["aieb_business", "brain_vectors", "default", "prod_documents", "Hybrid_Upper"],
)
def test_drop_refuses_business_collection(collection: str) -> None:
    store = make_store(collection)
    with pytest.raises(VectorStoreError) as excinfo:
        store.drop()
    assert "不在允许自动删除的前缀" in str(excinfo.value)
    # 关键：客户端一次都没被调用
    assert store._client.deleted == []


@pytest.mark.parametrize("prefix", DROPPABLE_PREFIXES)
def test_drop_allows_benchmark_prefixes(prefix: str) -> None:
    store = make_store(f"{prefix}something")
    store.drop()
    assert store._client.deleted == [f"{prefix}something"]


def test_drop_allows_any_when_explicit() -> None:
    store = make_store("aieb_business")
    store.drop(allow_any=True)
    assert store._client.deleted == ["aieb_business"]


def test_default_prefix_list_matches_documented_set() -> None:
    """防止有人悄悄把业务集合名前缀加进白名单。"""
    assert set(DROPPABLE_PREFIXES) == {
        "bench_",
        "scale_",
        "hybrid_",
        "emb_",
        "pytest_",
        "test_",
    }
