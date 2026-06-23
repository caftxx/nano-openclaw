"""Zvec-backed memory_search provider.

The provider is optional and lazy-loaded. Installing nano-openclaw without the
``zvec`` extra keeps the default lexical provider unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Iterable

from nano_openclaw.features.memory.providers import (
    MemorySearchProvider,
    MemorySearchRequest,
    ProviderSearchResult,
)


_CONTENT_FIELD = "content"
_PATH_FIELD = "path"
_START_LINE_FIELD = "start_line"
_END_LINE_FIELD = "end_line"
_CONTENT_HASH_FIELD = "content_hash"
_MTIME_NS_FIELD = "mtime_ns"
_DENSE_VECTOR = "dense_embedding"
_MANIFEST_FILE = "nano-openclaw-memory-manifest.json"
_MAX_CHUNK_CHARS = 1600
_MIN_CHUNK_CHARS = 240


@dataclass(frozen=True)
class _ZvecOptions:
    mode: str
    path: Path
    tokenizer_name: str
    default_operator: str
    auto_index: bool
    optimize_after_upsert: bool
    dense_embedder: str | None
    dense_metric: str
    dense_score_kind: str
    rrf_rank_constant: float
    local_dense: dict[str, Any]

    @property
    def uses_fts(self) -> bool:
        return self.mode in {"fts", "hybrid"}

    @property
    def uses_dense(self) -> bool:
        return self.mode in {"dense", "hybrid"}


@dataclass(frozen=True)
class _MemoryChunk:
    id: str
    path: str
    content: str
    start_line: int
    end_line: int
    content_hash: str
    mtime_ns: int


class ZvecMemorySearchProvider(MemorySearchProvider):
    """Local Zvec memory provider.

    Supported modes:
    - fts: native Zvec FTS/BM25 over memory file chunks.
    - dense: dense vector search using a configured embedding function.
    - hybrid: app-side RRF merge of FTS and dense routes.
    """

    @property
    def name(self) -> str:
        return "zvec"

    def is_available(self) -> bool:
        try:
            importlib.import_module("zvec")
        except Exception:
            return False
        return True

    def search(
        self,
        request: MemorySearchRequest,
        *,
        workspace_dir: str,
        config: Any | None = None,
        now: datetime | None = None,
    ) -> list[ProviderSearchResult]:
        zvec = importlib.import_module("zvec")
        workspace = Path(workspace_dir)
        options = _resolve_options(config, workspace)
        dense = _create_dense_embedder(options) if options.uses_dense else None
        collection = _open_or_create_collection(zvec, options, dense)

        if options.auto_index:
            _sync_index(zvec, collection, workspace, options, dense)

        routes: list[list[ProviderSearchResult]] = []
        if options.uses_fts:
            routes.append(_query_fts(zvec, collection, request, workspace, options))
        if options.uses_dense and dense is not None:
            routes.append(_query_dense(zvec, collection, request, workspace, options, dense))

        if not routes:
            return []

        if len(routes) == 1:
            results = _normalize_single_route(routes[0], request, options)
        else:
            results = _merge_rrf(routes, request, options)

        results = _apply_temporal_decay(results, config, now=now)
        results.sort(key=lambda item: item.score, reverse=True)
        return results[: request.max_results]


def _resolve_options(config: Any | None, workspace: Path) -> _ZvecOptions:
    provider_cfg = _provider_config(config, "zvec")
    mode = str(provider_cfg.get("mode") or "fts").lower()
    if mode not in {"fts", "dense", "hybrid"}:
        mode = "fts"

    path_value = provider_cfg.get("path")
    if path_value:
        path = Path(str(path_value)).expanduser()
        if not path.is_absolute():
            path = workspace / path
    else:
        path = workspace / ".nano-openclaw" / "memory-zvec" / mode

    bm25_cfg = _as_dict(provider_cfg.get("bm25"))
    fts_cfg = _as_dict(provider_cfg.get("fts"))
    language = str(bm25_cfg.get("language") or fts_cfg.get("language") or "").lower()
    tokenizer = str(fts_cfg.get("tokenizer") or fts_cfg.get("tokenizerName") or "")
    if not tokenizer:
        tokenizer = "jieba" if language == "zh" else "standard"

    dense_embedder = provider_cfg.get("denseEmbedder")
    if dense_embedder is None and mode in {"dense", "hybrid"}:
        dense_embedder = "local_dense"
    if dense_embedder:
        dense_embedder = str(dense_embedder)

    rerank_cfg = _as_dict(provider_cfg.get("rerank"))
    local_dense_cfg = _as_dict(provider_cfg.get("localDense"))

    return _ZvecOptions(
        mode=mode,
        path=path,
        tokenizer_name=tokenizer,
        default_operator=str(fts_cfg.get("defaultOperator") or "OR").upper(),
        auto_index=bool(provider_cfg.get("autoIndex", True)),
        optimize_after_upsert=bool(provider_cfg.get("optimizeAfterUpsert", False)),
        dense_embedder=dense_embedder,
        dense_metric=str(provider_cfg.get("denseMetric") or "cosine").lower(),
        dense_score_kind=str(provider_cfg.get("denseScore") or "distance").lower(),
        rrf_rank_constant=float(rerank_cfg.get("rankConstant") or 60),
        local_dense=local_dense_cfg,
    )


def _provider_config(config: Any | None, provider_name: str) -> dict[str, Any]:
    providers = None
    if isinstance(config, dict):
        providers = config.get("providers")
    elif config is not None:
        providers = getattr(config, "providers", None)
    if isinstance(providers, dict):
        value = providers.get(provider_name)
        if isinstance(value, dict):
            return value
    return {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _open_or_create_collection(zvec: Any, options: _ZvecOptions, dense: Any | None) -> Any:
    options.path.parent.mkdir(parents=True, exist_ok=True)
    if options.path.exists() and any(options.path.iterdir()):
        return zvec.open(
            path=str(options.path),
            option=zvec.CollectionOption(read_only=False, enable_mmap=True),
        )

    schema = _build_schema(zvec, options, dense)
    return zvec.create_and_open(path=str(options.path), schema=schema)


def _build_schema(zvec: Any, options: _ZvecOptions, dense: Any | None) -> Any:
    fields = [
        zvec.FieldSchema(name=_PATH_FIELD, data_type=zvec.DataType.STRING, nullable=False),
        zvec.FieldSchema(name=_START_LINE_FIELD, data_type=zvec.DataType.INT64, nullable=False),
        zvec.FieldSchema(name=_END_LINE_FIELD, data_type=zvec.DataType.INT64, nullable=False),
        zvec.FieldSchema(name=_CONTENT_HASH_FIELD, data_type=zvec.DataType.STRING, nullable=False),
        zvec.FieldSchema(name=_MTIME_NS_FIELD, data_type=zvec.DataType.INT64, nullable=False),
    ]
    if options.uses_fts:
        fields.append(
            zvec.FieldSchema(
                name=_CONTENT_FIELD,
                data_type=zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.FtsIndexParam(
                    tokenizer_name=options.tokenizer_name,
                    filters=["lowercase"],
                ),
            )
        )
    else:
        fields.append(
            zvec.FieldSchema(name=_CONTENT_FIELD, data_type=zvec.DataType.STRING, nullable=False)
        )

    vectors = []
    if options.uses_dense and dense is not None:
        metric = zvec.MetricType.COSINE if options.dense_metric == "cosine" else zvec.MetricType.IP
        vectors.append(
            zvec.VectorSchema(
                name=_DENSE_VECTOR,
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=int(dense.dimension),
                index_param=zvec.HnswIndexParam(metric_type=metric),
            )
        )

    return zvec.CollectionSchema(name="nano_openclaw_memory", fields=fields, vectors=vectors)


def _create_dense_embedder(options: _ZvecOptions) -> Any | None:
    if options.dense_embedder != "local_dense":
        return None
    try:
        from zvec.extension import DefaultLocalDenseEmbedding
    except Exception:
        return None

    kwargs: dict[str, Any] = {}
    model_source = options.local_dense.get("modelSource")
    if model_source:
        kwargs["model_source"] = str(model_source)
    return DefaultLocalDenseEmbedding(**kwargs)


def _sync_index(
    zvec: Any,
    collection: Any,
    workspace: Path,
    options: _ZvecOptions,
    dense: Any | None,
) -> None:
    manifest_path = options.path / _MANIFEST_FILE
    manifest = _read_manifest(manifest_path)
    current_files = {chunk.path for chunk in _iter_memory_chunks(workspace)}
    indexed_files = set(manifest.get("files", {}))

    for removed in sorted(indexed_files - current_files):
        old_ids = manifest.get("files", {}).get(removed, {}).get("chunk_ids", [])
        if old_ids:
            collection.delete(ids=old_ids)
        manifest.get("files", {}).pop(removed, None)

    upserts = []
    files_manifest = manifest.setdefault("files", {})
    for file_path in _memory_file_paths(workspace):
        rel_path = file_path.relative_to(workspace).as_posix()
        stat = _safe_stat(file_path)
        if stat is None:
            continue
        file_record = files_manifest.get(rel_path, {})
        if (
            file_record.get("mtime_ns") == stat.st_mtime_ns
            and file_record.get("size") == stat.st_size
        ):
            continue

        old_ids = file_record.get("chunk_ids", [])
        if old_ids:
            collection.delete(ids=old_ids)

        chunks = list(_chunks_for_file(file_path, workspace, stat.st_mtime_ns))
        files_manifest[rel_path] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "chunk_ids": [chunk.id for chunk in chunks],
        }
        upserts.extend(_chunk_to_doc(zvec, chunk, options, dense) for chunk in chunks)

    if upserts:
        collection.upsert(upserts)
        if options.optimize_after_upsert:
            collection.optimize()
    _write_manifest(manifest_path, manifest)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _memory_file_paths(workspace: Path) -> list[Path]:
    paths: list[Path] = []
    root_memory = workspace / "MEMORY.md"
    if root_memory.exists():
        paths.append(root_memory)
    memory_dir = workspace / "memory"
    if memory_dir.exists():
        paths.extend(sorted(memory_dir.glob("*.md")))
    return paths


def _iter_memory_chunks(workspace: Path) -> Iterable[_MemoryChunk]:
    for path in _memory_file_paths(workspace):
        stat = _safe_stat(path)
        if stat is None:
            continue
        yield from _chunks_for_file(path, workspace, stat.st_mtime_ns)


def _safe_stat(path: Path) -> Any | None:
    try:
        return path.stat()
    except OSError:
        return None


def _chunks_for_file(file_path: Path, workspace: Path, mtime_ns: int) -> Iterable[_MemoryChunk]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    rel_path = file_path.relative_to(workspace).as_posix()
    lines = content.splitlines()
    if not lines:
        return

    start = 0
    buffer: list[str] = []
    for index, line in enumerate(lines):
        candidate = "\n".join(buffer + [line])
        if buffer and len(candidate) > _MAX_CHUNK_CHARS:
            yield _make_chunk(rel_path, buffer, start, index - 1, mtime_ns)
            start = index
            buffer = [line]
            continue
        if not line.strip() and len(candidate) >= _MIN_CHUNK_CHARS:
            yield _make_chunk(rel_path, buffer + [line], start, index, mtime_ns)
            start = index + 1
            buffer = []
            continue
        buffer.append(line)

    if buffer:
        yield _make_chunk(rel_path, buffer, start, len(lines) - 1, mtime_ns)


def _make_chunk(
    rel_path: str,
    lines: list[str],
    start_index: int,
    end_index: int,
    mtime_ns: int,
) -> _MemoryChunk:
    content = "\n".join(lines).strip()
    digest = hashlib.sha1(
        f"{rel_path}:{start_index + 1}:{end_index + 1}:{content}".encode("utf-8")
    ).hexdigest()
    return _MemoryChunk(
        id=digest,
        path=rel_path,
        content=content,
        start_line=start_index + 1,
        end_line=end_index + 1,
        content_hash=hashlib.sha1(content.encode("utf-8")).hexdigest(),
        mtime_ns=mtime_ns,
    )


def _chunk_to_doc(zvec: Any, chunk: _MemoryChunk, options: _ZvecOptions, dense: Any | None) -> Any:
    vectors: dict[str, Any] = {}
    if options.uses_dense and dense is not None:
        vectors[_DENSE_VECTOR] = dense.embed(chunk.content)
    return zvec.Doc(
        id=chunk.id,
        fields={
            _PATH_FIELD: chunk.path,
            _CONTENT_FIELD: chunk.content,
            _START_LINE_FIELD: chunk.start_line,
            _END_LINE_FIELD: chunk.end_line,
            _CONTENT_HASH_FIELD: chunk.content_hash,
            _MTIME_NS_FIELD: chunk.mtime_ns,
        },
        vectors=vectors,
    )


def _query_fts(
    zvec: Any,
    collection: Any,
    request: MemorySearchRequest,
    workspace: Path,
    options: _ZvecOptions,
) -> list[ProviderSearchResult]:
    Fts = getattr(zvec, "Fts", None)
    Query = getattr(zvec, "Query", None)
    if Fts is None or Query is None:
        query_mod = importlib.import_module("zvec.model.param.query")
        Fts = getattr(query_mod, "Fts")
        Query = getattr(query_mod, "Query")

    query_kwargs: dict[str, Any] = {
        "field_name": _CONTENT_FIELD,
        "fts": Fts(match_string=request.query),
    }
    fts_param = getattr(zvec, "FtsQueryParam", None)
    if fts_param is not None:
        query_kwargs["param"] = fts_param(default_operator=options.default_operator)

    docs = collection.query(
        queries=Query(**query_kwargs),
        topk=request.max_results,
        output_fields=[
            _PATH_FIELD,
            _CONTENT_FIELD,
            _START_LINE_FIELD,
            _END_LINE_FIELD,
        ],
    )
    return [_doc_to_result(doc, workspace, request.context_lines, route="fts") for doc in docs]


def _query_dense(
    zvec: Any,
    collection: Any,
    request: MemorySearchRequest,
    workspace: Path,
    options: _ZvecOptions,
    dense: Any,
) -> list[ProviderSearchResult]:
    vector = dense.embed(request.query)
    docs = collection.query(
        queries=zvec.Query(field_name=_DENSE_VECTOR, vector=vector),
        topk=request.max_results,
        include_vector=False,
        output_fields=[
            _PATH_FIELD,
            _CONTENT_FIELD,
            _START_LINE_FIELD,
            _END_LINE_FIELD,
        ],
    )
    return [_doc_to_result(doc, workspace, request.context_lines, route="dense") for doc in docs]


def _doc_to_result(
    doc: Any,
    workspace: Path,
    context_lines: int,
    *,
    route: str,
) -> ProviderSearchResult:
    fields = getattr(doc, "fields", None) or {}
    path = str(fields.get(_PATH_FIELD) or "")
    start_line = _to_int(fields.get(_START_LINE_FIELD), 1)
    end_line = _to_int(fields.get(_END_LINE_FIELD), start_line)
    snippet, start_line, end_line = _snippet_with_context(
        workspace,
        path,
        start_line,
        end_line,
        str(fields.get(_CONTENT_FIELD) or ""),
        context_lines,
    )
    raw_score = float(getattr(doc, "score", 0.0) or 0.0)
    return ProviderSearchResult(
        path=path,
        snippet=snippet,
        score=raw_score,
        raw_score=raw_score,
        start_line=start_line,
        end_line=end_line,
        provider=f"zvec:{route}",
    )


def _snippet_with_context(
    workspace: Path,
    rel_path: str,
    start_line: int,
    end_line: int,
    fallback: str,
    context_lines: int,
) -> tuple[str, int, int]:
    path = workspace / rel_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return fallback, start_line, end_line

    if not lines:
        return fallback, start_line, end_line
    start = max(1, start_line - context_lines)
    end = min(len(lines), end_line + context_lines)
    return "\n".join(lines[start - 1:end]), start, end


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_single_route(
    results: list[ProviderSearchResult],
    request: MemorySearchRequest,
    options: _ZvecOptions,
) -> list[ProviderSearchResult]:
    if not results:
        return []

    if options.mode == "dense" and options.dense_score_kind == "distance":
        for item in results:
            item.score = round(1 / (1 + max(item.raw_score or 0.0, 0.0)), 4)
    else:
        max_score = max(abs(item.score) for item in results) or 1.0
        for item in results:
            item.score = round(item.score / max_score, 4)

    results = [item for item in results if item.score >= request.min_score]
    results.sort(key=lambda item: item.score, reverse=True)
    return results[: request.max_results]


def _merge_rrf(
    routes: list[list[ProviderSearchResult]],
    request: MemorySearchRequest,
    options: _ZvecOptions,
) -> list[ProviderSearchResult]:
    by_key: dict[tuple[str, int, int], ProviderSearchResult] = {}
    scores: dict[tuple[str, int, int], float] = {}
    for route in routes:
        for rank, item in enumerate(route, start=1):
            key = (item.path, item.start_line, item.end_line)
            by_key.setdefault(key, item)
            scores[key] = scores.get(key, 0.0) + 1 / (options.rrf_rank_constant + rank)

    if not scores:
        return []

    max_score = max(scores.values()) or 1.0
    merged: list[ProviderSearchResult] = []
    for key, raw in scores.items():
        item = by_key[key]
        item.raw_score = raw
        item.score = round(raw / max_score, 4)
        item.provider = "zvec:hybrid"
        if item.score >= request.min_score:
            merged.append(item)

    merged.sort(key=lambda item: item.score, reverse=True)
    return merged[: request.max_results]


def _apply_temporal_decay(
    results: list[ProviderSearchResult],
    config: Any | None,
    now: datetime | None = None,
) -> list[ProviderSearchResult]:
    try:
        from nano_openclaw.features.memory.tools import _apply_temporal_decay as apply_decay
    except Exception:
        return results
    return apply_decay(results, config, now=now)
