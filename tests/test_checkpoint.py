from __future__ import annotations

from pathlib import Path

from nano_openclaw.checkpoint import create_checkpoint, list_checkpoints, restore_checkpoint


def test_checkpoint_create_and_restore(tmp_path: Path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("before", encoding="utf-8")

    cp = create_checkpoint(state_dir, workspace, reason="test")
    assert cp is not None
    target.write_text("after", encoding="utf-8")

    checkpoints = list_checkpoints(state_dir)
    assert [c.id for c in checkpoints] == [cp.id]

    restored = restore_checkpoint(
        state_dir,
        cp.id[:12],
        workspace_dir=workspace,
        create_pre_restore=False,
    )

    assert restored is not None
    assert target.read_text(encoding="utf-8") == "before"
