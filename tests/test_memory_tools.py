"""Tests for memory tools: memory_get and memory_search.

Mirrors openclaw's memory-core/src/tools.ts behavior:
- memory_get: read specific memory files or excerpts
- memory_search: lexical search across MEMORY.md + memory/*.md
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
import sys
import tempfile
import types
from pathlib import Path

import pytest

from nano_openclaw.features.memory.tools import (
    memory_get,
    memory_search,
    MemorySearchResult,
    memory_search_provider_names,
    register_memory_search_provider,
    _apply_temporal_decay,
)
from nano_openclaw.features.memory.providers import MemorySearchProvider, MemorySearchRequest
from nano_openclaw.config.types import MemorySearchConfig, NanoOpenClawConfig, PluginsConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.core.tools import build_core_registry


def _dispatch_result(registry, *args):
    result = registry.dispatch(*args)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


@pytest.fixture
def workspace_with_memory_files():
    """Create a workspace with MEMORY.md and daily memory files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)

        # Create MEMORY.md
        (ws / "MEMORY.md").write_text(
            "# Long-term Memory\n\n"
            "## Key Decisions\n"
            "- Use lexical search for simplicity\n"
            "- Keep nano lightweight\n\n"
            "## Preferences\n"
            "- Python code style: concise\n"
            "- Test frequently\n"
        )

        # Create memory/ directory with daily files
        memory_dir = ws / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-05-02.md").write_text(
            "# Daily log\n\n"
            "Implemented memory feature today.\n"
            "Added memory_get and memory_search tools.\n"
        )
        (memory_dir / "2026-05-01.md").write_text(
            "# Yesterday\n\n"
            "Started planning memory implementation.\n"
        )

        yield str(ws)


class _FakeZvecFieldSchema:
    def __init__(self, name, data_type, nullable=False, index_param=None):
        self.name = name
        self.data_type = data_type
        self.nullable = nullable
        self.index_param = index_param


class _FakeZvecVectorSchema:
    def __init__(self, name, data_type, dimension=None, index_param=None):
        self.name = name
        self.data_type = data_type
        self.dimension = dimension
        self.index_param = index_param


class _FakeZvecCollectionSchema:
    def __init__(self, name, fields=None, vectors=None):
        self.name = name
        self.fields = fields or []
        self.vectors = vectors or []


class _FakeZvecDoc:
    def __init__(self, id, fields=None, vectors=None, score=0.0):
        self.id = id
        self.fields = fields or {}
        self.vectors = vectors or {}
        self.score = score


class _FakeZvecFtsIndexParam:
    def __init__(self, tokenizer_name="standard", filters=None, extra_params=""):
        self.tokenizer_name = tokenizer_name
        self.filters = filters or []
        self.extra_params = extra_params


class _FakeZvecCollectionOption:
    def __init__(self, read_only=False, enable_mmap=True):
        self.read_only = read_only
        self.enable_mmap = enable_mmap


class _FakeZvecQuery:
    def __init__(self, field_name, fts=None, vector=None, param=None):
        self.field_name = field_name
        self.fts = fts
        self.vector = vector
        self.param = param


class _FakeZvecFts:
    def __init__(self, match_string=None, query_string=None):
        self.match_string = match_string
        self.query_string = query_string


class _FakeZvecCollection:
    def __init__(self):
        self.docs = {}

    def upsert(self, docs):
        if not isinstance(docs, list):
            docs = [docs]
        for doc in docs:
            self.docs[doc.id] = doc
        return [{"code": 0} for _ in docs]

    def delete(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        for doc_id in ids:
            self.docs.pop(doc_id, None)
        return {"code": 0}

    def optimize(self):
        return {"code": 0}

    def query(self, queries, topk=10, **kwargs):
        query_text = ""
        if queries.fts is not None:
            query_text = queries.fts.match_string or queries.fts.query_string or ""
        terms = [term.lower() for term in query_text.split() if term.strip()]
        scored = []
        for doc in self.docs.values():
            content = str(doc.fields.get("content") or "").lower()
            score = sum(1 for term in terms if term in content)
            if score > 0:
                scored.append(_FakeZvecDoc(doc.id, fields=doc.fields, vectors={}, score=float(score)))
        scored.sort(key=lambda doc: doc.score, reverse=True)
        return scored[:topk]


def _install_fake_zvec(monkeypatch):
    collections = {}
    fake = types.ModuleType("zvec")
    fake.created_schemas = []
    fake.DataType = types.SimpleNamespace(
        STRING="string",
        INT64="int64",
        VECTOR_FP32="vector_fp32",
    )
    fake.MetricType = types.SimpleNamespace(COSINE="cosine", IP="ip")
    fake.FieldSchema = _FakeZvecFieldSchema
    fake.VectorSchema = _FakeZvecVectorSchema
    fake.CollectionSchema = _FakeZvecCollectionSchema
    fake.FtsIndexParam = _FakeZvecFtsIndexParam
    fake.HnswIndexParam = lambda **kwargs: kwargs
    fake.CollectionOption = _FakeZvecCollectionOption
    fake.Doc = _FakeZvecDoc
    fake.Query = _FakeZvecQuery
    fake.Fts = _FakeZvecFts

    def create_and_open(path, schema):
        collection = _FakeZvecCollection()
        collections[path] = collection
        fake.created_schemas.append(schema)
        return collection

    def open_collection(path, option=None):
        return collections[path]

    fake.create_and_open = create_and_open
    fake.open = open_collection
    monkeypatch.setitem(sys.modules, "zvec", fake)
    return fake


class TestMemoryGet:
    """Tests for memory_get tool."""

    def test_read_memory_md(self, workspace_with_memory_files):
        """Should read MEMORY.md file."""
        result = memory_get({"path": "MEMORY.md"}, workspace_with_memory_files)

        assert "[MEMORY.md]" in result
        assert "Long-term Memory" in result
        assert "Key Decisions" in result

    def test_read_daily_memory(self, workspace_with_memory_files):
        """Should read daily memory file."""
        result = memory_get({"path": "memory/2026-05-02.md"}, workspace_with_memory_files)

        assert "[memory/2026-05-02.md]" in result
        assert "Daily log" in result

    def test_read_with_line_range(self, workspace_with_memory_files):
        """Should read specific line range."""
        result = memory_get(
            {"path": "MEMORY.md", "from": 1, "lines": 3},
            workspace_with_memory_files
        )

        assert "[MEMORY.md lines 1-3]" in result
        assert "1:" in result  # Line numbers should be present

    def test_file_not_found(self, workspace_with_memory_files):
        """Should return error for missing file."""
        result = memory_get({"path": "nonexistent.md"}, workspace_with_memory_files)

        assert "[file not found:" in result

    def test_no_workspace_directory(self):
        """Should return error when no workspace directory."""
        result = memory_get({"path": "MEMORY.md"}, None)

        assert "[error: no workspace directory]" in result

    def test_path_escaping_workspace(self, workspace_with_memory_files):
        """Should reject paths that escape workspace."""
        result = memory_get({"path": "../outside.md"}, workspace_with_memory_files)

        # Should either error or not read the file
        assert "error" in result.lower() or "not found" in result.lower()


class TestMemorySearch:
    """Tests for memory_search tool."""

    def test_search_single_keyword(self, workspace_with_memory_files):
        """Should find matches for single keyword."""
        result = memory_search({"query": "preferences"}, workspace_with_memory_files)

        assert "Memory search results:" in result
        assert "MEMORY.md" in result
        assert "preferences" in result.lower()

    def test_search_multiple_keywords(self, workspace_with_memory_files):
        """Should find matches across files for multiple keywords."""
        result = memory_search({"query": "memory feature"}, workspace_with_memory_files)

        assert "Memory search results:" in result
        # Should find in daily files
        assert "2026-05" in result

    def test_search_max_results(self, workspace_with_memory_files):
        """Should respect maxResults parameter."""
        result = memory_search(
            {"query": "memory", "maxResults": 2},
            workspace_with_memory_files
        )

        # Count result lines (each result has 2 lines)
        lines = result.split("\n")
        result_lines = [l for l in lines if l.startswith("- ")]
        assert len(result_lines) <= 2

    def test_search_min_score(self, workspace_with_memory_files):
        """Should respect minScore parameter."""
        # High minScore should filter out weak matches
        result = memory_search(
            {"query": "xyz nonexistent keyword", "minScore": 0.5},
            workspace_with_memory_files
        )

        assert "no matches found" in result.lower()

    def test_search_empty_query(self, workspace_with_memory_files):
        """Should handle empty query."""
        result = memory_search({"query": ""}, workspace_with_memory_files)

        assert "results" in result.lower()

    def test_search_no_workspace(self):
        """Should return error when no workspace."""
        result = memory_search({"query": "test"}, None)

        assert "error" in result.lower() or "no workspace" in result.lower()

    def test_search_includes_line_numbers(self, workspace_with_memory_files):
        """Should include line numbers in results."""
        result = memory_search({"query": "decisions"}, workspace_with_memory_files)

        assert "MEMORY.md" in result
        # Line numbers format: path:start-end
        assert ":" in result  # Should have line separator

    def test_default_provider_is_lexical(self):
        """memory_search should expose lexical as the built-in provider."""
        assert "lexical" in memory_search_provider_names()

    def test_zvec_provider_is_registered(self):
        """The optional Zvec provider should be selectable when installed."""
        assert "zvec" in memory_search_provider_names()

    def test_configured_provider_can_override_search(self, workspace_with_memory_files):
        """Configured providers should run behind the stable memory_search tool."""

        class DummyProvider(MemorySearchProvider):
            @property
            def name(self) -> str:
                return "dummy-test"

            def search(self, request: MemorySearchRequest, *, workspace_dir: str, config=None, now=None):
                return [
                    MemorySearchResult(
                        path="memory/dummy.md",
                        snippet=f"dummy hit for {request.query}",
                        score=0.99,
                        start_line=1,
                        end_line=1,
                    )
                ]

        register_memory_search_provider(DummyProvider())
        result = memory_search(
            {"query": "decisions"},
            workspace_with_memory_files,
            config={"provider": "dummy-test"},
        )

        assert "memory/dummy.md:1-1" in result
        assert "dummy hit for decisions" in result

    def test_unknown_provider_falls_back_to_lexical(self, workspace_with_memory_files):
        """A typo in memorySearch.provider should not break memory_search."""
        result = memory_search(
            {"query": "decisions"},
            workspace_with_memory_files,
            config={"provider": "missing-provider"},
        )

        assert "Memory search results:" in result
        assert "MEMORY.md" in result

    def test_temporal_decay_disabled_by_default(self):
        """Temporal decay should not apply unless explicitly configured."""
        results = [
            MemorySearchResult("memory/2026-01-01.md", "old", 1.0, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            MemorySearchConfig(),
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 1.0

    def test_temporal_decay_halves_score_at_half_life(self):
        """A dated daily note 30 days old should score at roughly 50%."""
        results = [
            MemorySearchResult("memory/2026-01-01.md", "old", 0.8, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == pytest.approx(0.4, abs=0.01)
        assert decayed[0].raw_score == 0.8

    def test_temporal_decay_can_change_search_ranking(self, tmp_path):
        """An older stronger raw lexical hit can rank below a newer daily note."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-01-01.md").write_text("# alpha beta\nalpha beta\n", encoding="utf-8")
        (memory_dir / "2026-01-31.md").write_text("alpha beta\n", encoding="utf-8")

        result = memory_search(
            {"query": "alpha beta", "maxResults": 2},
            str(tmp_path),
            config={"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        result_lines = [line for line in result.splitlines() if line.startswith("- ")]
        assert result_lines[0].startswith("- memory/2026-01-31.md")
        assert result_lines[1].startswith("- memory/2026-01-01.md")

    def test_temporal_decay_does_not_decay_evergreen_memory_paths(self):
        """MEMORY.md and non-dated memory/*.md files are evergreen."""
        results = [
            MemorySearchResult("MEMORY.md", "root", 1.0, 1, 1),
            MemorySearchResult("memory/projects.md", "topic", 0.75, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 1.0
        assert decayed[1].score == 0.75

    def test_temporal_decay_future_dates_do_not_boost_or_decay(self):
        """Future dated daily notes are treated as age zero."""
        results = [
            MemorySearchResult("memory/2099-01-01.md", "future", 0.9, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 0.9

    def test_zvec_provider_fts_indexes_and_searches(self, tmp_path, monkeypatch):
        """Zvec FTS mode should build a local index and return memory hits."""
        fake_zvec = _install_fake_zvec(monkeypatch)
        (tmp_path / "MEMORY.md").write_text(
            "# Project Memory\n\nUse zvec FTS for optional memory search.\n",
            encoding="utf-8",
        )

        result = memory_search(
            {"query": "zvec memory"},
            str(tmp_path),
            config={
                "provider": "zvec",
                "providers": {
                    "zvec": {
                        "mode": "fts",
                        "path": ".nano-openclaw/test-zvec",
                        "bm25": {"language": "en"},
                    }
                },
            },
        )

        assert "Memory search results:" in result
        assert "MEMORY.md" in result
        assert "zvec FTS" in result
        assert fake_zvec.created_schemas[0].fields[-1].index_param.tokenizer_name == "standard"

    def test_zvec_provider_uses_jieba_for_chinese_bm25(self, tmp_path, monkeypatch):
        """Chinese BM25 config should map to Zvec's jieba tokenizer."""
        fake_zvec = _install_fake_zvec(monkeypatch)
        (tmp_path / "MEMORY.md").write_text("记忆 搜索 使用 zvec\n", encoding="utf-8")

        memory_search(
            {"query": "记忆"},
            str(tmp_path),
            config={
                "provider": "zvec",
                "providers": {
                    "zvec": {
                        "mode": "fts",
                        "path": ".nano-openclaw/test-zvec-zh",
                        "bm25": {"language": "zh"},
                    }
                },
            },
        )

        assert fake_zvec.created_schemas[0].fields[-1].index_param.tokenizer_name == "jieba"

    def test_zvec_provider_incrementally_updates_changed_memory(self, tmp_path, monkeypatch):
        """Changed memory files should replace old Zvec chunks on the next search."""
        _install_fake_zvec(monkeypatch)
        memory_md = tmp_path / "MEMORY.md"
        memory_md.write_text("oldtoken decision\n", encoding="utf-8")
        config = {
            "provider": "zvec",
            "providers": {
                "zvec": {
                    "mode": "fts",
                    "path": ".nano-openclaw/test-zvec-incremental",
                }
            },
        }

        first = memory_search({"query": "oldtoken"}, str(tmp_path), config=config)
        memory_md.write_text("newtoken changed decision\n", encoding="utf-8")
        second = memory_search({"query": "newtoken"}, str(tmp_path), config=config)
        old_after_update = memory_search({"query": "oldtoken"}, str(tmp_path), config=config)

        assert "oldtoken" in first
        assert "newtoken" in second
        assert "no matches found" in old_after_update.lower()


class TestMemorySearchKnobs:
    """Tests for memory_search interface knobs: contextLines, caseSensitive, outputMode."""

    @pytest.fixture
    def workspace_with_case_content(self):
        """Workspace where keyword exists only in mixed case (uppercase)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "MEMORY.md").write_text(
                "# Notes\n\n"
                "Line one of context.\n"
                "Line two of context.\n"
                "Here is the SpecialKeyword on a line.\n"
                "Line four of context.\n"
                "Line five of context.\n"
                "Line six of context.\n"
                "Line seven of context.\n",
                encoding="utf-8",
            )
            yield str(ws)

    def test_context_lines_larger_value_yields_more_lines(self, workspace_with_case_content):
        """contextLines=5 should produce a longer snippet window than default 2."""
        default_result = memory_search(
            {"query": "SpecialKeyword"},
            workspace_with_case_content,
        )
        wide_result = memory_search(
            {"query": "SpecialKeyword", "contextLines": 5},
            workspace_with_case_content,
        )

        # Find the line-range marker, e.g. "MEMORY.md:3-7"
        def _range_span(text: str) -> int:
            for token in text.split():
                if token.startswith("MEMORY.md:"):
                    rng = token.split(":", 1)[1].split()[0]
                    start_s, end_s = rng.split("-")
                    return int(end_s) - int(start_s)
            return -1

        default_span = _range_span(default_result)
        wide_span = _range_span(wide_result)
        assert default_span >= 0
        assert wide_span > default_span

    def test_case_sensitive_true_misses_lowercase_query(self, workspace_with_case_content):
        """caseSensitive=true with lowercase query should not match uppercase text."""
        result = memory_search(
            {"query": "specialkeyword", "caseSensitive": True},
            workspace_with_case_content,
        )
        assert "no matches found" in result.lower()

    def test_case_sensitive_false_matches_regardless_of_case(self, workspace_with_case_content):
        """caseSensitive=false (default) should match across cases."""
        result = memory_search(
            {"query": "specialkeyword", "caseSensitive": False},
            workspace_with_case_content,
        )
        assert "Memory search results:" in result
        assert "MEMORY.md" in result

    def test_output_mode_paths_only_excludes_snippet(self, workspace_with_case_content):
        """outputMode=paths_only should not emit snippet bodies or line ranges."""
        result = memory_search(
            {"query": "SpecialKeyword", "outputMode": "paths_only"},
            workspace_with_case_content,
        )
        assert "Memory search results:" in result
        assert "MEMORY.md" in result
        # No snippet text body in paths_only mode
        assert "SpecialKeyword" not in result
        # No line-range marker in paths_only mode
        assert "MEMORY.md:" not in result

    def test_output_mode_count_summary(self, workspace_with_case_content):
        """outputMode=count should emit a single 'N files, M hits' line."""
        result = memory_search(
            {"query": "SpecialKeyword", "outputMode": "count"},
            workspace_with_case_content,
        )
        assert "files" in result
        assert "hits" in result
        assert "1 files" in result
        assert "1 hits" in result

    def test_output_modes_share_ranking(self, workspace_with_memory_files):
        """The same query under all three output modes hits the same files in the same order."""
        query = {"query": "memory"}
        snippet = memory_search({**query, "outputMode": "snippet"}, workspace_with_memory_files)
        paths = memory_search({**query, "outputMode": "paths_only"}, workspace_with_memory_files)

        def _paths_in_order(text: str) -> list[str]:
            seen: list[str] = []
            for line in text.splitlines():
                if not line.startswith("- "):
                    continue
                token = line[2:].split(":", 1)[0].split(" (")[0]
                if token not in seen:
                    seen.append(token)
            return seen

        snippet_order = _paths_in_order(snippet)
        paths_order = _paths_in_order(paths)
        assert snippet_order == paths_order
        assert len(snippet_order) > 0

    def test_context_lines_overflow_is_clamped(self, workspace_with_case_content):
        """contextLines=999 should not error and should be clamped to file bounds (<= 20)."""
        result = memory_search(
            {"query": "SpecialKeyword", "contextLines": 999},
            workspace_with_case_content,
        )
        assert "Memory search results:" in result
        assert "MEMORY.md" in result

    def test_invalid_output_mode_falls_back_to_snippet(self, workspace_with_case_content):
        """outputMode='bogus' should silently degrade to snippet behavior."""
        result = memory_search(
            {"query": "SpecialKeyword", "outputMode": "bogus"},
            workspace_with_case_content,
        )
        # Snippet mode emits line range + snippet body
        assert "MEMORY.md:" in result
        assert "SpecialKeyword" in result


class TestMemorySearchResult:
    """Tests for MemorySearchResult dataclass."""

    def test_dataclass_fields(self):
        """Should have all expected fields."""
        result = MemorySearchResult(
            path="MEMORY.md",
            snippet="test snippet",
            score=0.8,
            start_line=5,
            end_line=5,
        )

        assert result.path == "MEMORY.md"
        assert result.snippet == "test snippet"
        assert result.score == 0.8
        assert result.raw_score == 0.8
        assert result.start_line == 5
        assert result.end_line == 5


class TestToolIntegration:
    """Tests for memory tools in registry."""

    def _registry(self):
        registry = build_core_registry()
        load_plugins(PluginsConfig(load=["memory"]), registry, NanoOpenClawConfig())
        return registry

    def test_tools_registered(self):
        """memory_get and memory_search should be registered."""
        registry = self._registry()

        names = registry.names()
        assert "memory_get" in names
        assert "memory_search" in names

    def test_memory_get_via_dispatch(self, workspace_with_memory_files):
        """Should be able to call memory_get via registry.dispatch."""
        registry = self._registry()
        registry.set_workspace_dir(workspace_with_memory_files)

        result = _dispatch_result(
            registry,
            "test-id",
            "memory_get",
            {"path": "MEMORY.md"}
        )

        assert result.get("is_error") is None
        text = result["content"][0]["text"]
        assert "Long-term Memory" in text

    def test_memory_search_via_dispatch(self, workspace_with_memory_files):
        """Should be able to call memory_search via registry.dispatch."""
        registry = self._registry()
        registry.set_workspace_dir(workspace_with_memory_files)

        result = _dispatch_result(
            registry,
            "test-id",
            "memory_search",
            {"query": "decisions"}
        )

        assert result.get("is_error") is None
        text = result["content"][0]["text"]
        assert "Memory search" in text
