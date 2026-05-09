"""Civil Engineering Domain Pack v0.1.

Recognises and processes construction / infrastructure documents:
BOQs, drawings, inspection reports, method statements, structural
calculations, tender documents, RFIs, NCRs, and more.

The pack is loaded via `build_civil_engineering_pack()` — pure
factory function, no side effects beyond reading the bundled
`domain.yaml` data file. Tests can build their own pack by passing
a different YAML path to `_load_pack_data`."""

from j1.domains.civil_engineering.pack import build_civil_engineering_pack

__all__ = ["build_civil_engineering_pack"]
