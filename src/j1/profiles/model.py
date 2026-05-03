from dataclasses import dataclass, field
from typing import Any

from j1.errors.exceptions import ProfileLoadError


@dataclass(frozen=True)
class Profile:
    profile_id: str
    metadata: dict[str, Any]
    prompts: dict[str, str] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_rules: dict[str, Any] = field(default_factory=dict)
    graph_taxonomy: dict[str, Any] = field(default_factory=dict)
    query_routing: dict[str, Any] = field(default_factory=dict)
    report_templates: dict[str, str] = field(default_factory=dict)

    @property
    def classification(self) -> dict[str, Any]:
        return self.metadata.get("classification", {}) or {}

    @property
    def confidence_rules(self) -> dict[str, Any]:
        return self.metadata.get("confidence", {}) or {}

    @property
    def display_name(self) -> str:
        return self.metadata.get("display_name", self.profile_id)

    def get_prompt(self, name: str) -> str:
        try:
            return self.prompts[name]
        except KeyError as exc:
            raise ProfileLoadError(
                f"prompt {name!r} not found in profile {self.profile_id!r}"
            ) from exc

    def get_schema(self, name: str) -> dict[str, Any]:
        try:
            return self.schemas[name]
        except KeyError as exc:
            raise ProfileLoadError(
                f"schema {name!r} not found in profile {self.profile_id!r}"
            ) from exc

    def get_report_template(self, name: str) -> str:
        try:
            return self.report_templates[name]
        except KeyError as exc:
            raise ProfileLoadError(
                f"report template {name!r} not found in profile {self.profile_id!r}"
            ) from exc
