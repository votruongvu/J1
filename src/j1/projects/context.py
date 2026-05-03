from dataclasses import dataclass

from j1._validators import validate_identifier


@dataclass(frozen=True)
class ProjectContext:
    tenant_id: str
    project_id: str
    profile: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.tenant_id, field="tenant_id")
        validate_identifier(self.project_id, field="project_id")
        if self.profile is not None:
            validate_identifier(self.profile, field="profile")
