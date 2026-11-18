"""Keep the installation guide's local links and copyable HA examples usable."""

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml
from homeassistant.components.automation.config import PLATFORM_SCHEMA
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import Template

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    *(ROOT / name for name in ("README.md", "README.de.md", "SECURITY.md", "TRADEMARK.md")),
    *sorted((ROOT / "docs").rglob("*.md")),
]
EXAMPLES = [
    pytest.param(ROOT / "docs" / "examples.md", id="en"),
    pytest.param(ROOT / "docs" / "de" / "examples.md", id="de"),
]


def yaml_examples(path):
    return [yaml.safe_load(block) for block in re.findall(r"```yaml\n(.*?)\n```", path.read_text(), re.S)]


def markdown_anchors(text):
    """GitHub-style anchors for the plain ATX headings used in these guides."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    counts = {}
    anchors = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.MULTILINE):
        slug = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        anchors.add(f"{slug}-{count}" if count else slug)
        counts[slug] = count + 1
    return anchors


def test_markdown_anchors_ignore_code_and_handle_repeated_headings():
    assert markdown_anchors(
        "# Überblick\n## `amzettel.sync_events`\n## Überblick\n```yaml\n# Nicht verlinken\n```\n"
    ) == {"überblick", "amzettelsync_events", "überblick-1"}


def test_documentation_local_links_exist():
    for page in PAGES:
        text = page.read_text()
        targets = re.findall(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text)
        for target in targets:
            if "://" in target:
                continue
            path, _, fragment = unquote(target).partition("#")
            destination = page.parent / path if path else page
            assert destination.exists(), f"{page.relative_to(ROOT)}: {target}"
            if fragment and destination.suffix == ".md":
                assert fragment in markdown_anchors(destination.read_text()), f"{page.relative_to(ROOT)}: {target}"


def test_user_documentation_has_no_internal_or_obsolete_material():
    assert not (ROOT / "docs" / "testing").exists()
    for directory in (ROOT / "docs", ROOT / "docs" / "de"):
        assert not (directory / "releasing.md").exists()
    forbidden = (
        "/Users/", "app-dev.amzettel.at", "auth-test.cocoar.dev",
        "SignalR", "screenshots/modgud-", "releasing.md",
    )
    for page in PAGES:
        text = page.read_text()
        for old in forbidden:
            assert old not in text, f"{page.relative_to(ROOT)}: {old}"


def test_user_documentation_has_no_internal_publication_status():
    forbidden = (
        "not yet published", "not published", "has not been published",
        "noch nicht veröffentlicht", "nicht veröffentlicht",
        "release notes – draft", "release-notizen – entwurf",
        "not decided yet", "noch nicht festgelegt",
        "after publication", "nach veröffentlichung",
        "provisional manifest", "vorläufige manifest",
    )
    for page in PAGES:
        text = " ".join(page.read_text().casefold().split())
        for status in forbidden:
            assert status not in text, f"{page.relative_to(ROOT)}: {status}"


def test_installation_guides_use_the_configured_repository_address():
    manifest = json.loads((ROOT / "custom_components" / "amzettel" / "manifest.json").read_text())
    for directory in (ROOT / "docs", ROOT / "docs" / "de"):
        assert f"`{manifest['documentation']}`" in (directory / "installation.md").read_text()


def test_documentation_indexes_show_branded_sign_in_and_the_ha_calendar():
    for directory in (ROOT / "docs", ROOT / "docs" / "de"):
        index = (directory / "README.md").read_text()
        for target in ("installation.md", "configuration.md", "actions.md", "examples.md", "troubleshooting.md"):
            assert f"]({target})" in index
        for image in ("amzettel-login.png", "calendar-week.png"):
            assert f"](screenshots/{image})" in index


def test_user_guides_link_to_the_other_language():
    for stem in (
        "README",
        "installation",
        "configuration",
        "actions",
        "examples",
        "troubleshooting",
        "branding",
        "release-notes",
    ):
        english = ROOT / "docs" / f"{stem}.md"
        german = ROOT / "docs" / "de" / f"{stem}.md"
        assert f"[Deutsch](de/{stem}.md)" in english.read_text()
        assert f"[English](../{stem}.md)" in german.read_text()
    assert "[Deutsch](README.de.md)" in (ROOT / "README.md").read_text()
    assert "[English](README.md)" in (ROOT / "README.de.md").read_text()


def test_english_is_the_default_documentation_language():
    assert not (ROOT / "README.en.md").exists()
    assert not (ROOT / "docs" / "en").exists()
    assert "# amZettel for Home Assistant\n" in (ROOT / "README.md").read_text()
    assert "# amZettel für Home Assistant\n" in (ROOT / "README.de.md").read_text()
    headings = {
        "README": "amZettel for Home Assistant – documentation",
        "actions": "Actions and events",
        "branding": "Brand images",
        "configuration": "Configuration and use",
        "examples": "Examples",
        "installation": "Installation and connection",
        "release-notes": "Release notes",
        "troubleshooting": "Troubleshooting",
    }
    for stem, heading in headings.items():
        assert (ROOT / "docs" / f"{stem}.md").read_text().startswith(f"# {heading}\n")
    assert "(docs/README.md)" in (ROOT / "README.md").read_text()
    assert "(docs/de/README.md)" in (ROOT / "README.de.md").read_text()
    assert "[Project overview](../README.md)" in (ROOT / "docs" / "README.md").read_text()
    assert "[Projektübersicht](../../README.de.md)" in (ROOT / "docs" / "de" / "README.md").read_text()


def test_screenshot_sets_match_and_are_referenced():
    english = ROOT / "docs"
    german = english / "de"
    images = {path.name for path in (german / "screenshots").glob("*.png")}
    assert images
    assert images == {path.name for path in (english / "screenshots").glob("*.png")}
    for directory in (german, english):
        guides = "\n".join((directory / name).read_text() for name in ("installation.md", "configuration.md"))
        for name in images:
            assert f"(screenshots/{name})" in guides
            assert (directory / "screenshots" / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("path", EXAMPLES)
async def test_examples_pass_home_assistant_schemas(hass, path):
    for example in yaml_examples(path):
        if "triggers" in example:
            PLATFORM_SCHEMA(example)
        else:
            cv.SCRIPT_SCHEMA(example.get("sequence", [example]))


@pytest.mark.parametrize("path", EXAMPLES)
async def test_calendar_import_template_builds_unique_sync_inputs(hass, path):
    automation = next(
        example
        for example in yaml_examples(path)
        if "triggers" in example and example["actions"][-1]["action"] == "amzettel.sync_events"
    )
    fetch = automation["actions"][0]
    mapping = automation["actions"][-1]["data"]["events"]
    rendered = Template(mapping, hass).async_render(
        {
            fetch["response_variable"]: {
                fetch["target"]["entity_id"]: {
                    "events": [
                        {"uid": "gelb", "summary": "Gelbe Tonne", "start": "2026-11-26", "end": "2026-11-27"},
                        {"summary": "Papier", "start": "2026-11-28"},
                    ]
                }
            }
        }
    )
    assert rendered == [
        {"external_id": "gelb|2026-11-26", "summary": "Gelbe Tonne", "start": "2026-11-26", "end": "2026-11-27"},
        {"external_id": "Papier|2026-11-28", "summary": "Papier", "start": "2026-11-28"},
    ]


@pytest.mark.parametrize(
    ("path", "empty_text"),
    [
        pytest.param(ROOT / "docs" / "examples.md", "Nothing planned yet.", id="en"),
        pytest.param(ROOT / "docs" / "de" / "examples.md", "Noch nichts geplant.", id="de"),
    ],
)
@pytest.mark.parametrize("has_meals", [False, True])
async def test_meal_plan_template_handles_empty_and_planned_days(hass, path, empty_text, has_meals):
    script = next(example for example in yaml_examples(path) if "sequence" in example)
    fetch = script["sequence"][0]
    message = script["sequence"][-1]["data"]["message"]
    meals = [{"dishes": [{"name": "Pasta"}, {"name": "Salad"}]}] if has_meals else []
    rendered = Template(message, hass).async_render(
        {fetch["response_variable"]: {fetch["target"]["entity_id"]: {"meals": meals}}}
    )
    assert rendered.strip() == ("Pasta, Salad" if has_meals else empty_text)
