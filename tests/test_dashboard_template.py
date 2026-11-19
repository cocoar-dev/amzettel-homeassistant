"""Keep the optional frontend example separate from integration/runtime data."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "dashboard"


def test_dashboard_language_presets_share_the_same_entities_and_layout():
    english = yaml.safe_load((EXAMPLE / "dashboard.yaml").read_text())
    german = yaml.safe_load((EXAMPLE / "dashboard.de.yaml").read_text())
    en_view = english["views"][0]
    de_view = german["views"][0]
    en_card, de_card = en_view["cards"][0], de_view["cards"][0]
    assert en_view["panel"] is de_view["panel"] is True
    assert en_card.pop("language") == "en"
    assert de_card.pop("language") == "de"
    assert en_card == de_card == {
        "type": "custom:amzettel-household-card",
        "household": "",
        "calendar": "calendar.household",
        "meals": "calendar.meal_plan",
        "shopping": "todo.shopping",
    }
    themes = yaml.safe_load((EXAMPLE / "theme.yaml").read_text())
    assert en_view["theme"] == de_view["theme"] == "amZettelHousehold"
    assert en_view["theme"] in themes


def test_dashboard_example_contains_no_machine_account_or_runtime_data():
    forbidden = re.compile(
        r"sheldon|/Users/|127\.0\.0\.1:|refresh_token|access_token|code_verifier"
        r"|amzettel\.auth\.|showcase-live|prod-demo",
        re.IGNORECASE,
    )
    for path in EXAMPLE.rglob("*"):
        if not path.is_file():
            continue
        assert path.suffix in {".md", ".mjs", ".yaml", ".png"}, path
        if path.suffix != ".png":
            assert not forbidden.search(path.read_text()), path


def test_both_readmes_feature_the_matching_language_preview_and_template():
    for name, preview, guide in (
        ("README.md", "preview.png", "README.md"),
        ("README.de.md", "preview.de.png", "README.de.md"),
    ):
        text = (ROOT / name).read_text()
        assert f"](examples/dashboard/{preview})" in text
        assert f"](examples/dashboard/{guide})" in text
        instructions = (EXAMPLE / guide).read_text()
        assert "amzettel-household-card.mjs?v=1" in instructions
        assert "amzettel-household-model.mjs" in instructions
        assert f"]({preview})" in instructions
        assert "](../../LICENSE)" in instructions
        assert "](../../TRADEMARK.md)" in instructions


def test_ci_runs_dashboard_tests_without_installing_frontend_packages():
    workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
    steps = workflow["jobs"]["dashboard"]["steps"]
    assert any(step.get("with", {}).get("node-version") == "22" for step in steps)
    assert [step["run"] for step in steps if "run" in step] == [
        "node --test examples/dashboard/tests/*.test.mjs"
    ]
