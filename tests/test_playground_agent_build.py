import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.playground.agent import build_playground_agent
from app.playground.sandbox import SkillSandboxBackend


def test_build_playground_agent_does_not_pass_permissions_with_executable_backend(tmp_path):
    llm = FakeListChatModel(responses=["ok"])
    sandbox = {
        "id": "sandbox_1",
        "name": "Sandbox",
        "status": "ready",
        "path": str(tmp_path / "sandbox"),
        "venv_path": str(tmp_path / "sandbox" / "venv"),
    }

    with patch("app.playground.agent.create_deep_agent", return_value=object()) as create:
        build_playground_agent(
            llm=llm,
            agent_config={
                "main_agent": {
                    "name": "Main",
                    "sandbox_id": "sandbox_1",
                    "enabled_skills": [],
                    "enabled_mcps": [],
                    "enabled_subagents": [],
                },
                "subagents": [],
            },
            runtime_root=str(tmp_path / "runtime"),
            sandbox_map={"sandbox_1": sandbox},
            main_sandbox_id="sandbox_1",
        )

    kwargs = create.call_args.kwargs
    assert isinstance(kwargs["backend"], SkillSandboxBackend)
    assert "permissions" not in kwargs
