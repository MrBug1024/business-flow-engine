import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playground import resources


def _resource_root(tmp_path: Path, user_id: str) -> Path:
    return tmp_path / resources._safe(user_id) / "resources"


def test_save_sandbox_ignores_client_paths(tmp_path, monkeypatch):
    user_id = "user/path"
    monkeypatch.setattr(resources, "_root", lambda uid: _resource_root(tmp_path, uid))

    item = resources.save_sandbox(
        user_id,
        {
            "name": "Data sandbox",
            "path": "D:/client-picked",
            "venv_path": "D:/client-picked/venv",
            "python": "D:/Python/python.exe",
        },
    )

    managed_root = (_resource_root(tmp_path, user_id) / "sandboxes" / item["id"]).resolve()
    assert Path(item["path"]) == managed_root
    assert Path(item["venv_path"]) == managed_root / "venv"
    assert item["python"] == sys.executable

    public = resources.public_sandbox_resource(item)
    assert public["storage_label"] == "平台托管"
    assert "path" not in public
    assert "venv_path" not in public
    assert "python" not in public


def test_list_sandboxes_migrates_legacy_paths(tmp_path, monkeypatch):
    user_id = "user/path"
    root = _resource_root(tmp_path, user_id)
    monkeypatch.setattr(resources, "_root", lambda uid: _resource_root(tmp_path, uid))
    sandbox_file = root / "sandboxes.json"
    sandbox_file.parent.mkdir(parents=True, exist_ok=True)
    sandbox_file.write_text(
        json.dumps(
            [
                {
                    "id": "sandbox_old",
                    "name": "Old",
                    "type": "python-venv",
                    "path": "D:/legacy",
                    "venv_path": "D:/legacy/venv",
                    "python": "D:/legacy/python.exe",
                    "status": "ready",
                    "error": "",
                    "dependencies": {"python": [], "node": {}},
                }
            ]
        ),
        encoding="utf-8",
    )

    rows = resources.list_sandboxes(user_id)

    managed_root = (root / "sandboxes" / "sandbox_old").resolve()
    assert len(rows) == 1
    assert Path(rows[0]["path"]) == managed_root
    assert Path(rows[0]["venv_path"]) == managed_root / "venv"
    assert rows[0]["python"] == sys.executable
    assert json.loads(sandbox_file.read_text(encoding="utf-8")) == rows
