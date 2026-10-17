import pytest
from unittest.mock import MagicMock, patch
from qdrant_client import QdrantClient
from backend import rag

@pytest.fixture(autouse=True)
def setup_mock_qdrant():
    # Spin up in-memory Qdrant client for fast, fully-offline testing
    in_memory_client = QdrantClient(":memory:")
    
    from qdrant_client.http import models
    in_memory_client.create_collection(
        collection_name=rag.COLLECTION_NAME,
        vectors_config=models.VectorParams(
            size=384,
            distance=models.Distance.COSINE
        )
    )
    
    # Mock embedding model to return mock vector representation
    mock_embedder = MagicMock()
    # Return a dummy vector of 384 dimensions for each text chunk input
    mock_embedder.embed.side_effect = lambda texts: [
        MagicMock(tolist=lambda: [0.1 if "кодовый" in text or "сейф" in text else 0.01] * 384) 
        for text in texts
    ]
    
    with patch("backend.rag.get_qdrant_client", return_value=in_memory_client), \
         patch("backend.rag.get_embedding_model", return_value=mock_embedder):
        yield

def test_chunk_text():
    text = "Hello " * 200 # ~1200 characters
    chunks = rag.chunk_text(text, chunk_size=600, overlap=150)
    
    assert len(chunks) >= 2
    assert chunks[0].startswith("Hello")
    assert len(chunks[0]) <= 600

def test_chunk_text_empty():
    assert rag.chunk_text("") == []
    assert rag.chunk_text("   ") == []

def test_index_and_search_rag():
    doc_id = "test-doc-123"
    title = "Секреты Тони"
    content = "Мой секретный кодовый ключ от сейфа: 1080-Star-Vexa."
    
    # Index document
    success = rag.index_document(doc_id, title, content)
    assert success is True
    
    # Search memory
    # Our mock embedder checks if "кодовый" is in query and returns matching vector weights
    hits = rag.search_memory("кодовый ключ от сейфа", limit=1)
    
    assert len(hits) == 1
    assert hits[0]["title"] == "Секреты Тони"
    assert hits[0]["doc_id"] == doc_id
    assert "1080-Star-Vexa" in hits[0]["content"]

def test_delete_document():
    doc_id = "test-doc-delete"
    rag.index_document(doc_id, "Note", "Some random notes to index")
    
    # Verify indexed
    docs = rag.list_documents()
    assert any(d["id"] == doc_id for d in docs)
    
    # Delete
    rag.delete_document(doc_id)
    
    # Verify deleted
    docs = rag.list_documents()
    assert not any(d["id"] == doc_id for d in docs)

def test_list_documents():
    rag.index_document("doc1", "Document One", "Content one")
    rag.index_document("doc2", "Document Two", "Content two")
    
    docs = rag.list_documents()
    assert len(docs) == 2
    titles = [d["title"] for d in docs]
    assert "Document One" in titles
    assert "Document Two" in titles


def test_private_sources_need_an_explicit_filter_and_agent_filter_scopes_them():
    rag.index_document("owner-doc", "Owner", "кодовый замок сейфа", source="manual")
    rag.index_document("learned-a", "Agent A note", "кодовый подход A", source="agent_learning",
                       extra_payload={"agent_id": "a"})
    rag.index_document("learned-b", "Agent B note", "кодовый подход B", source="agent_learning",
                       extra_payload={"agent_id": "b"})

    generic = rag.search_memory("кодовый", limit=5, threshold=0.1)
    assert {hit["doc_id"] for hit in generic} == {"owner-doc"}

    scoped = rag.search_memory("кодовый", limit=5, threshold=0.1,
                               source_filter="agent_learning", agent_filter="a")
    assert [hit["doc_id"] for hit in scoped] == ["learned-a"]
    assert scoped[0]["agent_id"] == "a"


def test_extra_payload_cannot_override_core_fields():
    rag.index_document("doc-1", "Title", "кодовый текст", source="manual",
                       extra_payload={"doc_id": "spoofed", "source": "obsidian"})
    hit = rag.search_memory("кодовый", limit=1, threshold=0.1)[0]
    assert hit["doc_id"] == "doc-1" and hit["source"] == "manual"
