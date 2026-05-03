from pathlib import Path

import pytest

from j1.errors.exceptions import ProfileLoadError, ProfileNotFoundError
from j1.profiles import (
    DEFAULT_PROFILE_ID,
    PROFILE_FILENAME,
    Profile,
    ProfileLoader,
    bundled_profiles_dir,
)


# ---- Helpers -----------------------------------------------------------


def _write_profile(
    base: Path,
    profile_id: str,
    *,
    profile_yaml: str = "profile_id: x\ndisplay_name: X\n",
    prompts: dict[str, str] | None = None,
    schemas: dict[str, str] | None = None,
    review_rules: str | None = None,
    graph_taxonomy: str | None = None,
    query_routing: str | None = None,
    report_templates: dict[str, str] | None = None,
) -> Path:
    profile_dir = base / profile_id
    profile_dir.mkdir(parents=True)
    (profile_dir / PROFILE_FILENAME).write_text(profile_yaml)
    if prompts:
        (profile_dir / "prompts").mkdir()
        for name, content in prompts.items():
            (profile_dir / "prompts" / name).write_text(content)
    if schemas:
        (profile_dir / "schemas").mkdir()
        for name, content in schemas.items():
            (profile_dir / "schemas" / name).write_text(content)
    if review_rules is not None:
        (profile_dir / "review_rules.yaml").write_text(review_rules)
    if graph_taxonomy is not None:
        (profile_dir / "graph_taxonomy.yaml").write_text(graph_taxonomy)
    if query_routing is not None:
        (profile_dir / "query_routing.yaml").write_text(query_routing)
    if report_templates:
        (profile_dir / "report_templates").mkdir()
        for name, content in report_templates.items():
            (profile_dir / "report_templates" / name).write_text(content)
    return profile_dir


# ---- Bundled default profile ------------------------------------------


def test_default_profile_is_bundled():
    assert (bundled_profiles_dir() / DEFAULT_PROFILE_ID / PROFILE_FILENAME).is_file()


def test_default_profile_loads():
    loader = ProfileLoader()
    profile = loader.load(DEFAULT_PROFILE_ID)
    assert isinstance(profile, Profile)
    assert profile.profile_id == DEFAULT_PROFILE_ID
    assert profile.display_name == "Default Profile"


def test_default_profile_has_empty_taxonomies():
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    assert profile.graph_taxonomy.get("node_types") == []
    assert profile.graph_taxonomy.get("edge_types") == []
    assert profile.review_rules.get("rules") == []
    assert profile.query_routing.get("routes") == []


def test_default_profile_includes_extract_prompt_and_schema():
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    assert "extract" in profile.prompts
    assert profile.prompts["extract"].strip().startswith("Extract")
    assert "artifact" in profile.schemas
    assert profile.schemas["artifact"]["type"] == "object"


# ---- Loader behavior ---------------------------------------------------


def test_unknown_profile_raises_clearly():
    loader = ProfileLoader()
    with pytest.raises(ProfileNotFoundError) as exc:
        loader.load("nonexistent")
    assert "nonexistent" in str(exc.value)


def test_custom_search_path(tmp_path):
    _write_profile(
        tmp_path,
        "domain_a",
        profile_yaml="profile_id: domain_a\ndisplay_name: Domain A\n",
        prompts={"summarize.md": "Summarize the input."},
    )
    loader = ProfileLoader(search_paths=[tmp_path])
    profile = loader.load("domain_a")
    assert profile.profile_id == "domain_a"
    assert profile.display_name == "Domain A"
    assert profile.prompts["summarize"] == "Summarize the input."


def test_custom_search_path_falls_back_to_bundled(tmp_path):
    loader = ProfileLoader(search_paths=[tmp_path])
    profile = loader.load(DEFAULT_PROFILE_ID)
    assert profile.profile_id == DEFAULT_PROFILE_ID


def test_user_path_takes_precedence_over_bundled(tmp_path):
    _write_profile(
        tmp_path,
        DEFAULT_PROFILE_ID,
        profile_yaml="profile_id: default\ndisplay_name: User Override\n",
    )
    loader = ProfileLoader(search_paths=[tmp_path])
    profile = loader.load(DEFAULT_PROFILE_ID)
    assert profile.display_name == "User Override"


def test_loader_caches_profiles():
    loader = ProfileLoader()
    a = loader.load(DEFAULT_PROFILE_ID)
    b = loader.load(DEFAULT_PROFILE_ID)
    assert a is b


def test_clear_cache_forces_reload():
    loader = ProfileLoader()
    a = loader.load(DEFAULT_PROFILE_ID)
    loader.clear_cache()
    b = loader.load(DEFAULT_PROFILE_ID)
    assert a is not b
    assert a == b


# ---- Error handling ----------------------------------------------------


def test_invalid_yaml_raises_load_error(tmp_path):
    profile_dir = tmp_path / "broken"
    profile_dir.mkdir()
    (profile_dir / PROFILE_FILENAME).write_text("not: valid: yaml: [\n")
    loader = ProfileLoader(search_paths=[tmp_path])
    with pytest.raises(ProfileLoadError):
        loader.load("broken")


def test_yaml_root_must_be_mapping(tmp_path):
    profile_dir = tmp_path / "list_root"
    profile_dir.mkdir()
    (profile_dir / PROFILE_FILENAME).write_text("- a\n- b\n")
    loader = ProfileLoader(search_paths=[tmp_path])
    with pytest.raises(ProfileLoadError):
        loader.load("list_root")


def test_invalid_schema_json_raises(tmp_path):
    _write_profile(
        tmp_path,
        "bad_schema",
        schemas={"broken.json": "{not valid json"},
    )
    loader = ProfileLoader(search_paths=[tmp_path])
    with pytest.raises(ProfileLoadError):
        loader.load("bad_schema")


def test_partial_profile_loads_with_only_profile_yaml(tmp_path):
    _write_profile(tmp_path, "minimal", profile_yaml="profile_id: minimal\n")
    profile = ProfileLoader(search_paths=[tmp_path]).load("minimal")
    assert profile.prompts == {}
    assert profile.schemas == {}
    assert profile.review_rules == {}
    assert profile.graph_taxonomy == {}


# ---- Profile API -------------------------------------------------------


def test_get_prompt_returns_content(tmp_path):
    _write_profile(
        tmp_path,
        "p",
        prompts={"summary.md": "Summarize."},
    )
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    assert profile.get_prompt("summary") == "Summarize."


def test_get_prompt_missing_raises(tmp_path):
    _write_profile(tmp_path, "p")
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    with pytest.raises(ProfileLoadError):
        profile.get_prompt("missing")


def test_get_schema_returns_dict(tmp_path):
    _write_profile(
        tmp_path,
        "p",
        schemas={"thing.json": '{"type": "object"}'},
    )
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    assert profile.get_schema("thing") == {"type": "object"}


def test_classification_property_reads_metadata(tmp_path):
    _write_profile(
        tmp_path,
        "p",
        profile_yaml=(
            "profile_id: p\n"
            "classification:\n"
            "  hints:\n"
            "    - mime_type=application/pdf\n"
        ),
    )
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    assert profile.classification == {"hints": ["mime_type=application/pdf"]}


def test_confidence_rules_property_reads_metadata(tmp_path):
    _write_profile(
        tmp_path,
        "p",
        profile_yaml=(
            "profile_id: p\n"
            "confidence:\n"
            "  default_threshold: 0.7\n"
        ),
    )
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    assert profile.confidence_rules == {"default_threshold": 0.7}


def test_report_templates_loaded(tmp_path):
    _write_profile(
        tmp_path,
        "p",
        report_templates={"summary.md": "# {{title}}\n"},
    )
    profile = ProfileLoader(search_paths=[tmp_path]).load("p")
    assert profile.get_report_template("summary") == "# {{title}}\n"


# ---- Processor integration --------------------------------------------


def test_processor_can_use_profile_settings():
    """A processor can be constructed with a Profile and access its data —
    proving profile data is reachable from processor code without core changes.
    """
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)

    class _ProfileAwareProcessor:
        kind = "test.profile_aware"

        def __init__(self, profile: Profile) -> None:
            self._profile = profile

        def used_prompt(self, name: str) -> str:
            return self._profile.get_prompt(name)

    processor = _ProfileAwareProcessor(profile)
    assert "Extract" in processor.used_prompt("extract")


def test_project_context_carries_profile_id():
    """ProjectContext.profile already exists; ProfileLoader uses that value.

    Sanity-check that the existing field works as the carrier for profile selection.
    """
    from j1.projects.context import ProjectContext

    ctx = ProjectContext(tenant_id="acme", project_id="alpha", profile=DEFAULT_PROFILE_ID)
    profile = ProfileLoader().load(ctx.profile or DEFAULT_PROFILE_ID)
    assert profile.profile_id == DEFAULT_PROFILE_ID
