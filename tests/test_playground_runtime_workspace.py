import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playground import resources


def _resource_root(tmp_path: Path, user_id: str) -> Path:
    return tmp_path / resources._safe(user_id) / "resources"


def _skill_source(tmp_path: Path, skill_id: str, skill_name: str) -> Path:
    src = tmp_path / "uploaded" / skill_id
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Test skill\n---\n\n# {skill_name}\n",
        encoding="utf-8",
    )
    return src


def test_prepare_runtime_workspace_copies_skills_under_spec_names(tmp_path, monkeypatch):
    user_id = "user/runtime"
    root = _resource_root(tmp_path, user_id)
    monkeypatch.setattr(resources, "_root", lambda uid: _resource_root(tmp_path, uid))

    main_src = _skill_source(tmp_path, "skill_main_id", "scenario-main")
    sub_src = _skill_source(tmp_path, "skill_sub_id", "business-data-query")
    (root / "skills.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "skills.json").write_text(
        json.dumps(
            [
                {
                    "id": "skill_main_id",
                    "name": "Scenario Main",
                    "skill_name": "scenario-main",
                    "description": "main",
                    "path": str(main_src),
                    "dependencies": {"python": [], "node": {}, "files": []},
                },
                {
                    "id": "skill_sub_id",
                    "name": "Business Query",
                    "skill_name": "business-data-query",
                    "description": "sub",
                    "path": str(sub_src),
                    "dependencies": {"python": [], "node": {}, "files": []},
                },
            ]
        ),
        encoding="utf-8",
    )

    runtime = resources.prepare_runtime_workspace(
        user_id,
        {
            "main_agent": {
                "enabled_skills": ["skill_main_id"],
                "enabled_subagents": ["sub_1"],
            },
            "subagents": [
                {
                    "id": "sub_1",
                    "enabled_skills": ["skill_sub_id"],
                }
            ],
        },
        tmp_path / "attachments",
    )

    runtime_root = Path(runtime["root"])
    assert (runtime_root / "skills" / "main" / "scenario-main" / "SKILL.md").exists()
    assert not (runtime_root / "skills" / "main" / "skill_main_id").exists()
    assert (
        runtime_root
        / "skills"
        / "subagents"
        / "sub_1"
        / "business-data-query"
        / "SKILL.md"
    ).exists()
    assert not (runtime_root / "skills" / "subagents" / "sub_1" / "skill_sub_id").exists()
    assert runtime["main_skill_sources"] == ["/skills/main"]
    assert runtime["subagent_skill_sources"] == {"sub_1": "/skills/subagents/sub_1"}
