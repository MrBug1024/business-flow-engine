import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.distillation.skill_builder import _render_node_runner
from app.domain.models import FlowStep


def test_node_runner_embeds_metadata_without_node_json():
    step = FlowStep(
        step_id=3,
        step_name="Filter rows",
        operation="FILTER",
        purpose="Keep matching rows",
        capability="Filter business rows",
        sql='SELECT * FROM input WHERE status = "ok"',
        output_columns=["id", "status"],
    )

    text = _render_node_runner(step)

    assert "node.json" not in text
    assert "__NODE_JSON_LITERAL__" not in text
    assert '"step_id": 3' in text
    assert "Filter business rows" in text
