import asyncio
import json

import httpx
import pytest

from app.ai.memory import DocumentIngestionError, DocumentMemoryService, EvidenceGraphRepository
from app.ai.providers import OpenAIEmbeddingProvider
from app.infrastructure.storage import LocalArtifactStore
from app.workspace.store import WorkspaceStore


class FakeEmbeddings:
    async def embed(self, texts):
        return {"model": "test-embedding", "dimensions": 1536, "vectors": [[0.0] * 1536 for _ in texts], "usage": {"total_tokens": 7}}


class WrongDimensions:
    async def embed(self, texts):
        return {"model": "test-embedding", "dimensions": 3, "vectors": [[0.0] * 3 for _ in texts], "usage": {}}


def test_openai_embedding_provider_uses_real_api_contract_without_live_call():
    seen = {}

    async def handler(request):
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "text-embedding-3-small", "data": [{"embedding": [0.0] * 3}], "usage": {"total_tokens": 2}})

    provider = OpenAIEmbeddingProvider(api_key="test", dimensions=3, transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.embed(["source text"]))

    assert seen["url"] == "https://api.openai.com/v1/embeddings"
    assert seen["payload"]["input"] == ["source text"]
    assert result["vectors"] == [[0.0] * 3]
    assert result["cost"] == "UNKNOWN"


def test_document_ingestion_preserves_provenance_and_content_addressed_artifact(tmp_path):
    store = WorkspaceStore(tmp_path / "documents.db")
    service = DocumentMemoryService(EvidenceGraphRepository(store), LocalArtifactStore(tmp_path / "artifacts"), FakeEmbeddings())

    result = asyncio.run(service.ingest(
        workspace_id="workspace-a", project_id=None, data=b"Utility confirmation is required for committed capacity.", media_type="text/plain",
        title="Utility letter", source_url="https://utility.example/letter", provider="Utility", retrieved_at=1_700_000_000,
        page="1", section="Capacity", jurisdiction="Texas", effective_date="2026-08-01",
    ))

    artifact = result["document"]["metadata"]["artifact"]
    assert artifact["role"] == "source-document"
    assert LocalArtifactStore(tmp_path / "artifacts").path(artifact).read_bytes().startswith(b"Utility")
    assert result["document"]["metadata"]["jurisdiction"] == "Texas"
    assert result["embedding"] == {"model": "test-embedding", "dimensions": 1536, "input_tokens": 7, "cost": "UNKNOWN"}


def test_document_ingestion_rejects_unsupported_parser_and_wrong_vector_dimension(tmp_path):
    store = WorkspaceStore(tmp_path / "documents.db")
    service = DocumentMemoryService(EvidenceGraphRepository(store), LocalArtifactStore(tmp_path / "artifacts"), FakeEmbeddings())
    with pytest.raises(DocumentIngestionError, match="No deterministic parser"):
        asyncio.run(service.ingest(workspace_id="workspace-a", project_id=None, data=b"%PDF", media_type="application/pdf", title="PDF", source_url="https://x", provider="Utility", retrieved_at=1))
    incompatible = DocumentMemoryService(EvidenceGraphRepository(store), LocalArtifactStore(tmp_path / "artifacts"), WrongDimensions())
    with pytest.raises(DocumentIngestionError, match="1536"):
        asyncio.run(incompatible.ingest(workspace_id="workspace-a", project_id=None, data=b"Capacity", media_type="text/plain", title="Letter", source_url="https://x", provider="Utility", retrieved_at=1))
