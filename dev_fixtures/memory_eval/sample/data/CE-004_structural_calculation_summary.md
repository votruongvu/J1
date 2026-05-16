# CE-004 — Structural Calculation Summary: Slab S-205 Deflection Check

Project: River Crossing Bridge Substructure
Element: Slab S-205, North Abutment
Drawing reference: D-302 revision B (see CE-005)
Calculated by: M. Okoye, Structural Engineer
Checked by: R. Patel, Senior Structural Engineer
Calculation Date: 2026-03-24

## Scope

This calculation summary documents the deflection check for slab
S-205. It is presented twice — once with the as-built concrete
grade C25/30 (which failed acceptance), and once with the
proposed re-pour concrete grade C40/50 prescribed by NCR-007.

The acceptance limit for total deflection is span/250 per
specification section 03 30 00 (see CE-005). The slab spans 8.6 m
in the primary direction, giving an acceptance limit of 34.4 mm.

## Load combination

ULS load combination 1.35 G + 1.5 Q is used for serviceability
deflection assessment per the project specification. The
characteristic permanent load G is 6.2 kN/m² and the
characteristic imposed load Q is 4.0 kN/m². The combined design
load for deflection is therefore w = 1.35 × 6.2 + 1.5 × 4.0 =
14.37 kN/m².

## Formula

Maximum deflection for a simply supported one-way slab under
uniform load:

  δ_max = 5 × w × L⁴ / (384 × E × I)

where:
* w is the uniform design load (kN/m²)
* L is the clear span (m)
* E is the modulus of elasticity of concrete (kN/m²)
* I is the second moment of area of the cracked section (m⁴)

The cracked section second moment of area is estimated as 0.4 of
the gross section value per the design code's deflection-control
clause.

## Case 1 — As-built concrete grade C25/30 (failed)

| Field | Value |
|---|---|
| Concrete grade | C25/30 |
| E (modulus of elasticity) | 31,000 N/mm² (31,000,000 kN/m²) |
| I (cracked, estimated) | 0.0018 m⁴ |
| w (design uniform load) | 14.37 kN/m² |
| L (clear span) | 8.6 m |
| δ_max (computed) | 38.7 mm |
| Acceptance limit (L/250) | 34.4 mm |
| Pass/Fail | FAIL |
| Margin | -12.5% (over limit) |

The C25/30 case fails the deflection acceptance criterion. This
test result is consistent with the inspection Finding F-12 and
NCR-007 — the lower-grade concrete is unsuitable for the slab
S-205 design.

## Case 2 — Proposed re-pour grade C40/50 (passed)

| Field | Value |
|---|---|
| Concrete grade | C40/50 |
| E (modulus of elasticity) | 35,000 N/mm² (35,000,000 kN/m²) |
| I (cracked, estimated) | 0.0019 m⁴ |
| w (design uniform load) | 14.37 kN/m² |
| L (clear span) | 8.6 m |
| δ_max (computed) | 32.4 mm |
| Acceptance limit (L/250) | 34.4 mm |
| Pass/Fail | PASS |
| Margin | +5.8% (under limit) |

The C40/50 re-pour case passes the deflection acceptance criterion
with positive margin. This calculation supports the corrective
action prescribed in NCR-007 in CE-003.

## Reinforcement check (informational)

The top reinforcement layout for slab S-205 is documented on
drawing D-302 revision B. The cover variance recorded as
inspection Finding F-13 (28-32 mm versus specified 40 mm) reduces
the effective depth by approximately 8-12 mm. A recomputed
flexural capacity check with 30 mm cover remains above the design
moment with a margin of 8%, but the project's durability
classification (XC4 exposure) requires the specified 40 mm cover
to be restored before sign-off.

## Conclusion

* Case 1 (C25/30) FAILS the deflection check.
* Case 2 (C40/50) PASSES the deflection check.
* The slab S-205 must be re-poured per NCR-007.
* Cover variance per Finding F-13 must also be resolved before
  the slab is signed off.

## Cross-references

* CE-002 site inspection Finding F-12 (concrete grade
  non-conformance) and Finding F-13 (reinforcement cover
  variance).
* CE-003 NCR-007 (corrective action: re-pour with C40/50).
* CE-005 drawing D-302 revision B and specification section
  03 30 00.
* CE-001 BOQ rows 03.05 (held open pending NCR closeout) and
  03.06 (re-pour quantity).
