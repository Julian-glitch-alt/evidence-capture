"""Review steps and answer fields per finding (T52, tasks/t52-design.md).

Pure: no database, no clock, no network. Applied when the claim detail is
built, never stored, so old claims gain their step and fields too.
"""

from . import proposal

STEPS = ("coverage", "liability", "damage", "estimate")

_COVERAGE = {"policy_on_file", "policy_period", "coverage",
             "vehicle_identity", "driver", "odometer"}
_LIABILITY = {"story", "multi_party", "injury", "loss_type", "fnol_statement",
              "police_report", "keys", "claim_type", "duplicate",
              "coaching", "evidence_missing"}
_DAMAGE = {"damage_location", "prior_damage", "screen_recapture"}
# A photo-quality or missing-analysis finding belongs to coverage when it is
# about an identity photo, else to damage.
_IDENTITY_PHOTOS = ("photo.plate", "photo.vin_photo", "photo.odometer", "photo.driver_license")

# Finding reason code -> the loss-notice answer ids it is about. Only ids the
# claim's answers actually contain are kept (fields_of filters).
FIELDS = {
    "VIN_MISMATCH": ("vin",),
    "VIN_NOT_PROVIDED": ("vin",),
    "DRIVER_NOT_LISTED": ("driver_is_holder",),
    "DRIVER_NOT_PROVIDED": ("driver_is_holder",),
    "LICENSE_NAME_MISMATCH": ("driver_is_holder",),
    "POLICE_REPORT_NEEDED": ("police_contacted",),
    "MULTI_PARTY": ("other_vehicle",),
    "INJURY_REPORTED": ("injuries",),
    "STORY_INCONSISTENT": ("description",),
    "STORY_UNCLEAR": ("description",),
    "FNOL_MISMATCH": ("description",),
    "DAMAGE_AREA_MISMATCH": ("damaged_areas",),
    "DAMAGE_AREA_UNCLEAR": ("damaged_areas",),
    "DAMAGE_OUTSIDE_STATEMENT": ("damaged_areas",),
    "PRIOR_DAMAGE_SUSPECTED": ("prior_damage",),
    "POLICY_NOT_IN_FORCE": ("loss_date",),
    "LOSS_DATE_NOT_PROVIDED": ("loss_date",),
}


def step_of(entry: dict) -> str:
    """One of STEPS, from the entry's root cause (unknown code = own cause)."""
    code = (entry or {}).get("reason_code")
    cause = proposal.ROOT_CAUSES.get(code, code)
    source = (entry or {}).get("source")
    if cause in _COVERAGE:
        return "coverage"
    if cause in _LIABILITY:
        return "liability"
    if cause in _DAMAGE:
        return "damage"
    if cause == "total_loss" or source == "estimate":
        return "estimate"
    if cause in ("photo_quality", "analysis_missing"):
        return "coverage" if source in _IDENTITY_PHOTOS else "damage"
    return "liability"


def fields_of(entry: dict, answers: dict | None) -> list[str]:
    """The form field ids the finding is about, kept only when answered."""
    have = answers or {}
    return [f for f in FIELDS.get((entry or {}).get("reason_code"), ()) if f in have]
