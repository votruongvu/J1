import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from j1.errors.exceptions import ProfileLoadError, ProfileNotFoundError
from j1.profiles.model import Profile

DEFAULT_PROFILE_ID = "default"
PROFILE_FILENAME = "profile.yaml"

REVIEW_RULES_FILENAME = "review_rules.yaml"
GRAPH_TAXONOMY_FILENAME = "graph_taxonomy.yaml"
QUERY_ROUTING_FILENAME = "query_routing.yaml"

PROMPTS_SUBDIR = "prompts"
SCHEMAS_SUBDIR = "schemas"
REPORT_TEMPLATES_SUBDIR = "report_templates"

_PROMPT_EXTENSIONS = {".md", ".txt"}
_SCHEMA_EXTENSIONS = {".json"}
_REPORT_EXTENSIONS = {".md", ".txt", ".tmpl", ".jinja", ".jinja2"}


def bundled_profiles_dir() -> Path:
    return Path(__file__).parent


class ProfileLoader:
    def __init__(self, search_paths: Iterable[Path] | None = None) -> None:
        if search_paths is None:
            paths = [bundled_profiles_dir()]
        else:
            paths = list(search_paths)
            if bundled_profiles_dir() not in paths:
                paths.append(bundled_profiles_dir())
        self._search_paths: list[Path] = paths
        self._cache: dict[str, Profile] = {}

    @property
    def search_paths(self) -> list[Path]:
        return list(self._search_paths)

    def load(self, profile_id: str) -> Profile:
        if profile_id in self._cache:
            return self._cache[profile_id]
        profile_dir = self._resolve(profile_id)
        profile = self._load_from_dir(profile_dir, profile_id)
        self._cache[profile_id] = profile
        return profile

    def clear_cache(self) -> None:
        self._cache.clear()

    def _resolve(self, profile_id: str) -> Path:
        for base in self._search_paths:
            candidate = base / profile_id
            if (candidate / PROFILE_FILENAME).is_file():
                return candidate
        searched = ", ".join(str(p) for p in self._search_paths)
        raise ProfileNotFoundError(
            f"profile {profile_id!r} not found in any search path ({searched})"
        )

    def _load_from_dir(self, profile_dir: Path, profile_id: str) -> Profile:
        metadata = _load_yaml(profile_dir / PROFILE_FILENAME, required=True)
        prompts = _load_text_files(profile_dir / PROMPTS_SUBDIR, _PROMPT_EXTENSIONS)
        schemas = _load_json_files(profile_dir / SCHEMAS_SUBDIR, _SCHEMA_EXTENSIONS)
        review_rules = _load_yaml(profile_dir / REVIEW_RULES_FILENAME)
        graph_taxonomy = _load_yaml(profile_dir / GRAPH_TAXONOMY_FILENAME)
        query_routing = _load_yaml(profile_dir / QUERY_ROUTING_FILENAME)
        report_templates = _load_text_files(
            profile_dir / REPORT_TEMPLATES_SUBDIR, _REPORT_EXTENSIONS
        )
        return Profile(
            profile_id=profile_id,
            metadata=metadata,
            prompts=prompts,
            schemas=schemas,
            review_rules=review_rules,
            graph_taxonomy=graph_taxonomy,
            query_routing=query_routing,
            report_templates=report_templates,
        )


def _load_yaml(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ProfileLoadError(f"required profile file missing: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ProfileLoadError(f"failed to parse YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProfileLoadError(
            f"YAML at {path} must be a mapping at the top level, got {type(data).__name__}"
        )
    return data


def _load_text_files(directory: Path, extensions: set[str]) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    results: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            results[path.stem] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileLoadError(f"failed to read {path}: {exc}") from exc
    return results


def _load_json_files(
    directory: Path, extensions: set[str]
) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileLoadError(f"failed to parse JSON at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ProfileLoadError(
                f"schema {path} must be a JSON object at the top level"
            )
        results[path.stem] = data
    return results
