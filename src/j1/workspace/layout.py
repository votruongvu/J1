from enum import StrEnum

TENANTS_DIR = "tenants"
PROJECTS_DIR = "projects"


class WorkspaceArea(StrEnum):
    RAW = "raw"
    COMPILED = "compiled"
    ENRICHED = "enriched"
    GRAPH = "graph"
    SEARCH = "search"
    AUDIT = "audit"
    RUNTIME = "runtime"
