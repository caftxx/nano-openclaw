from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nano_openclaw.features.skills import curator, usage


def test_usage_records_events(tmp_path: Path):
    usage.record_event(tmp_path, "debug-skill", "load", source="workspace", path="/x/SKILL.md")
    usage.record_event(tmp_path, "debug-skill", "use", source="workspace", path="/x/SKILL.md")

    rows = usage.report(tmp_path)

    assert len(rows) == 1
    assert rows[0]["name"] == "debug-skill"
    assert rows[0]["load_count"] == 1
    assert rows[0]["use_count"] == 1
    assert rows[0]["state"] == usage.STATE_ACTIVE
    assert rows[0]["activity_count"] == 2


def test_curator_dry_run_does_not_mutate_state(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    path = usage.usage_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "old-skill": {
            "name": "old-skill",
            "state": "active",
            "pinned": False,
            "created_at": old,
            "last_loaded_at": old,
            "load_count": 1,
        }
    }), encoding="utf-8")

    result = curator.run(
        tmp_path,
        curator.CuratorConfig(stale_after_days=30, archive_after_days=90),
        dry_run=True,
    )

    assert result["counts"]["marked_stale"] == 1
    assert usage.load_usage(tmp_path)["old-skill"]["state"] == "active"
    assert Path(result["report_path"]).exists()


def test_curator_run_marks_stale(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    path = usage.usage_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "old-skill": {
            "name": "old-skill",
            "state": "active",
            "pinned": False,
            "created_at": old,
            "last_loaded_at": old,
            "load_count": 1,
        }
    }), encoding="utf-8")

    result = curator.run(
        tmp_path,
        curator.CuratorConfig(stale_after_days=30, archive_after_days=90),
    )

    assert result["counts"]["marked_stale"] == 1
    assert usage.load_usage(tmp_path)["old-skill"]["state"] == "stale"
