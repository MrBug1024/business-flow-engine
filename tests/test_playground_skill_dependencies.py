import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playground.resources import _parse_skill_dependencies


def test_standard_skill_dependencies_are_inferred_without_requirements(tmp_path):
    skill = tmp_path / "xlsx"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: xlsx\ndescription: Work with spreadsheets\n---\n\n# XLSX\n",
        encoding="utf-8",
    )
    (scripts / "recalc.py").write_text(
        "import pandas as pd\nimport duckdb\n\nprint(pd.DataFrame().to_markdown())\n",
        encoding="utf-8",
    )

    deps = _parse_skill_dependencies(skill)

    assert not (skill / "requirements.txt").exists()
    assert "pandas>=2.2.0" in deps["python"]
    assert "duckdb>=1.1.0" in deps["python"]
    assert "tabulate>=0.9.0" in deps["python"]
