"""Source-document ingestion and hybrid retrieval for the evidence graph."""
from __future__ import annotations

import hashlib
import re
import time
from html.parser import HTMLParser
from typing import Any

from app.ai.contracts import EmbeddingProvider
from app.ai.memory.graph import EvidenceGraphRepository


class DocumentIngestionError(ValueError):
    pass


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _document_id(workspace_id: str, project_id: str | None, source_url: str, content_hash: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}:{project_id}:{source_url}:{content_hash}".encode()).hexdigest()
    return f"document_{digest[:24]}"


def _text(data: bytes, media_type: str) -> str:
    if media_type not in {"text/plain", "text/html"}:
        raise DocumentIngestionError(f"No deterministic parser is configured for {media_type}.")
    text = data.decode("utf-8", errors="strict")
    if media_type == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        text = " ".join(parser.parts)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise DocumentIngestionError("Document contains no extractable text.")
    return text


def _chunks(text: str, maximum_characters: int = 4_000) -> list[str]:
    """Stable paragraph-aware chunks; conservative size keeps embedding requests bounded."""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    result: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 1 > maximum_characters:
            result.append(current)
            current = ""
        while len(paragraph) > maximum_characters:
            result.append(paragraph[:maximum_characters])
            paragraph = paragraph[maximum_characters:]
        current = f"{current} {paragraph}".strip()
    if current:
        result.append(current)
    return result


class DocumentMemoryService:
    """Ingest immutable source files and combine vector candidates with graph records."""

    def __init__(self, graph: EvidenceGraphRepository, artifacts: Any, embeddings: EmbeddingProvider):
        self.graph, self.artifacts, self.embeddings = graph, artifacts, embeddings

    async def ingest(
        self,
        *,
        workspace_id: str,
        project_id: str | None,
        data: bytes,
        media_type: str,
        title: str,
        source_url: str,
        provider: str,
        retrieved_at: float,
        page: str | None = None,
        section: str | None = None,
        jurisdiction: str | None = None,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        if not all((workspace_id, title.strip(), source_url.strip(), provider.strip())):
            raise DocumentIngestionError("Document provenance requires workspace, title, source URL, and provider.")
        if project_id and self.graph._project(project_id).get("workspace_id") != workspace_id:
            raise DocumentIngestionError("Document workspace must match the project workspace.")
        text = _text(data, media_type)
        artifact = self.artifacts.put(data, extension="html" if media_type == "text/html" else "txt", media_type=media_type, role="source-document")
        document_id = _document_id(workspace_id, project_id, source_url, artifact["sha256"])
        parts = _chunks(text)
        embedding_result = await self.embeddings.embed(parts)
        model, dimensions, vectors = embedding_result["model"], embedding_result["dimensions"], embedding_result["vectors"]
        if dimensions != 1536:
            raise DocumentIngestionError("The current pgvector schema requires 1536-dimensional embeddings.")
        if len(vectors) != len(parts) or any(len(vector) != dimensions for vector in vectors):
            raise DocumentIngestionError("Embedding provider returned incomplete or inconsistent vectors.")
        now = time.time()
        metadata = {"title": title, "provider": provider, "retrieved_at": retrieved_at, "page": page, "section": section,
                    "jurisdiction": jurisdiction, "effective_date": effective_date, "artifact": artifact}
        document = {"document_id": document_id, "workspace_id": workspace_id, "project_id": project_id, "source_url": source_url,
                    "source_type": provider, "content_hash": artifact["sha256"], "metadata": metadata, "created_at": now}
        chunks = [{"chunk_id": f"{document_id}_{ordinal}", "ordinal": ordinal, "content": content, "source_metadata": metadata,
                   "embedding": vector, "embedding_model": model, "embedding_dimensions": dimensions, "created_at": now}
                  for ordinal, (content, vector) in enumerate(zip(parts, vectors))]
        self.graph.store_document(document, chunks)
        return {"document": document, "chunks": [{key: value for key, value in chunk.items() if key != "embedding"} for chunk in chunks],
                "embedding": {"model": model, "dimensions": dimensions, "input_tokens": (embedding_result.get("usage") or {}).get("total_tokens"), "cost": "UNKNOWN"}}

    async def retrieve(self, project_id: str, query: str, limit: int = 8, *, as_of: float | None = None) -> dict[str, Any]:
        """Hybrid retrieval: authoritative graph records plus pgvector document candidates."""
        graph_records = self.graph.find_relevant_memory(project_id, query, limit=limit)
        result = {"query": query, "as_of": as_of, "graph_records": graph_records, "document_chunks": [], "vector_queries": 0}
        if not self.graph._postgres:
            return result
        embedded = await self.embeddings.embed([query])
        result["vector_queries"] = 1
        result["document_chunks"] = self.graph.search_document_chunks(project_id, embedded["vectors"][0], limit=limit, as_of=as_of)
        return result
