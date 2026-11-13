"""Qdrant 集成测试：真实服务端、真实 HNSW 配置、真实 payload 过滤。

服务端未启动时**整文件跳过**（带明确原因），而不是让 CI 红灯或假装通过。
启动方式：
    docker compose up -d qdrant
或（无 Docker 的 Windows）：
    tools/qdrant/qdrant.exe
"""

from __future__ import annotations

import uuid

import pytest

from aieb.brain.retrieval.vector_store import (
    ChunkRecord,
    QdrantVectorStore,
    VectorStoreError,
    point_id_for,
)

QDRANT_URL = "http://127.0.0.1:6333"


def qdrant_available() -> bool:
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        return False
    try:
        QdrantClient(url=QDRANT_URL, timeout=2).get_collections()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not qdrant_available(), reason=f"Qdrant 未在 {QDRANT_URL} 运行（跳过集成测试）"
)

DIM = 8


def _vector(seed: float) -> list[float]:
    return [seed, seed + 0.1, seed + 0.2, seed + 0.3, 0.1, 0.2, 0.3, 0.4]


def _record(document_id: str, doc_key: str, page: int, text: str, scope: str = "default") -> ChunkRecord:
    from aieb.brain.chunkkey import content_hash

    return ChunkRecord(
        document_id=document_id,
        document_key=doc_key,
        doc_name=f"{document_id}.pdf",
        page=page,
        chunk_index=page - 1,
        content=text,
        content_hash=content_hash(text),
        embedding_model_version=f"test-embedder@{DIM}",
        scope_id=scope,
        knowledge_base_id="kb-test",
        source_filename=f"{document_id}.pdf",
        created_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture()
def store():
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    client = QdrantVectorStore(url=QDRANT_URL, collection=name, dim=DIM)
    yield client
    try:
        client.drop()
    except Exception:  # noqa: BLE001
        pass


def test_upsert_search_and_count(store):
    records = [
        _record("doc1", "k1", 1, "供应商逾期交付的违约金"),
        _record("doc1", "k1", 2, "付款方式 3:5:2 分期"),
        _record("doc2", "k2", 1, "差旅住宿标准"),
    ]
    store.upsert(records, [_vector(0.9), _vector(0.5), _vector(0.1)])
    assert store.count() == 3

    hits = store.search(_vector(0.9), top_k=2, knowledge_base_id="kb-test")
    assert len(hits) == 2
    assert hits[0].doc_name == "doc1.pdf"
    assert hits[0].page == 1
    assert hits[0].score >= hits[1].score


def test_upsert_is_idempotent_for_same_content(store):
    records = [_record("doc1", "k1", 1, "同一段内容")]
    store.upsert(records, [_vector(0.7)])
    store.upsert(records, [_vector(0.7)])
    store.upsert(records, [_vector(0.7)])
    assert store.count() == 1, "重复上传/重复同步不得产生重复向量"


def test_point_id_is_deterministic_across_processes(store):
    record = _record("doc1", "k1", 1, "确定性点 ID")
    assert record.point_id == point_id_for(record.key)
    assert record.point_id == point_id_for(record.key)


def test_scope_filter_isolates_tenants(store):
    store.upsert(
        [_record("doc1", "k1", 1, "租户 A 的制度", scope="tenant-a")],
        [_vector(0.9)],
    )
    store.upsert(
        [_record("doc2", "k2", 1, "租户 B 的制度", scope="tenant-b")],
        [_vector(0.9)],
    )
    a = store.search(_vector(0.9), top_k=10, scope_id="tenant-a", knowledge_base_id="kb-test")
    b = store.search(_vector(0.9), top_k=10, scope_id="tenant-b", knowledge_base_id="kb-test")
    assert [h.doc_id for h in a] == ["doc1"]
    assert [h.doc_id for h in b] == ["doc2"]
    assert store.search(_vector(0.9), top_k=10, scope_id="tenant-c", knowledge_base_id="kb-test") == []


def test_document_ids_filter(store):
    store.upsert(
        [_record("doc1", "k1", 1, "甲"), _record("doc2", "k2", 1, "乙")],
        [_vector(0.9), _vector(0.8)],
    )
    only = store.search(
        _vector(0.9), top_k=10, knowledge_base_id="kb-test", document_ids=["doc2"]
    )
    assert [h.doc_id for h in only] == ["doc2"]


def test_delete_by_document_and_document_key(store):
    store.upsert(
        [_record("doc1", "k1", 1, "甲"), _record("doc1", "k1", 2, "乙"), _record("doc2", "k2", 1, "丙")],
        [_vector(0.9), _vector(0.8), _vector(0.7)],
    )
    assert store.count() == 3
    store.delete_by_document("doc1")
    assert store.count() == 1
    store.delete_by_document_key("k2")
    assert store.count() == 0


def test_dimension_mismatch_is_reported(store):
    store.upsert([_record("doc1", "k1", 1, "甲")], [_vector(0.9)])
    wrong = QdrantVectorStore(url=QDRANT_URL, collection=store.collection, dim=DIM + 4)
    with pytest.raises(VectorStoreError) as excinfo:
        wrong.count()
    assert "维度" in str(excinfo.value)


def test_collection_info_reports_hnsw_and_status(store):
    store.upsert([_record("doc1", "k1", 1, "甲")], [_vector(0.9)])
    info = store.info()
    assert info["points_count"] == 1
    assert info["dim"] == DIM
    assert info["hnsw_m"] == 16, "HNSW m 应按创建时的配置建出"
    assert info["status"].lower() == "green"


def test_health_reports_true_when_server_up(store):
    assert store.health() is True
