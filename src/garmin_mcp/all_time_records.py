"""Deduplicated all-time records derived from Garmin activity summaries.

The implementation deliberately uses only ``get_activities``. Garmin already
stores standard running split times and power-duration peaks on activity
summaries, so a complete account scan needs one paginated request stream rather
than an N+1 split or FIT-file download loop.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


garmin_client = None

PAGE_SIZE = 100
MAX_PAGES = 1_000
ZWIFT_GARMIN_MAX_START_GAP_SECONDS = 15 * 60
ZWIFT_GARMIN_MAX_DURATION_DELTA = 0.30
ZWIFT_GARMIN_MIN_OVERLAP = 0.90
HALF_MARATHON_METERS = 21_097.5
HALF_MARATHON_MAX_EXTRA_DISTANCE = 0.01

RUN_SPLIT_FIELDS: Tuple[Tuple[str, str, int], ...] = (
    ("one_kilometer", "fastestSplit_1000", 1_000),
    ("five_kilometers", "fastestSplit_5000", 5_000),
    ("ten_kilometers", "fastestSplit_10000", 10_000),
)

POWER_DURATION_FIELDS: Tuple[Tuple[int, str], ...] = (
    (1, "maxAvgPower_1"),
    (2, "maxAvgPower_2"),
    (5, "maxAvgPower_5"),
    (10, "maxAvgPower_10"),
    (20, "maxAvgPower_20"),
    (30, "maxAvgPower_30"),
    (60, "maxAvgPower_60"),
    (120, "maxAvgPower_120"),
    (300, "maxAvgPower_300"),
    (600, "maxAvgPower_600"),
    (1_200, "maxAvgPower_1200"),
    (1_800, "maxAvgPower_1800"),
    (3_600, "maxAvgPower_3600"),
    (7_200, "maxAvgPower_7200"),
)

_RECORD_FIELDS = (
    "distance",
    "maxHR",
    "fastestSplit_1000",
    "fastestSplit_5000",
    "fastestSplit_10000",
    "maxPower",
    *(field for _, field in POWER_DURATION_FIELDS),
)


def configure(client) -> None:
    """Configure the module with the Garmin client instance."""
    global garmin_client
    garmin_client = client


def _number(value: Any, *, positive: bool = False) -> Optional[float]:
    """Return a finite numeric value while rejecting booleans and sentinels."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    if positive and converted <= 0:
        return None
    return converted


def _activity_type(activity: Dict[str, Any]) -> str:
    return str((activity.get("activityType") or {}).get("typeKey") or "").lower()


def _sport_family(activity: Dict[str, Any]) -> str:
    type_key = _activity_type(activity)
    if "running" in type_key or type_key in {"run", "track_run"}:
        return "running"
    if any(token in type_key for token in ("cycling", "ride", "biking")):
        return "cycling"
    if "swim" in type_key:
        return "swimming"
    return type_key


def _timestamp_seconds(activity: Dict[str, Any]) -> Optional[float]:
    timestamp_ms = _number(activity.get("beginTimestamp"))
    if timestamp_ms is not None and timestamp_ms > 0:
        return timestamp_ms / 1_000

    for field in ("startTimeGMT", "startTimeLocal"):
        value = activity.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            continue
    return None


def _interval(activity: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    start = _timestamp_seconds(activity)
    duration = _number(activity.get("duration"), positive=True)
    if start is None or duration is None:
        return None
    return start, start + duration, duration


def _manufacturer(activity: Dict[str, Any]) -> str:
    return str(activity.get("manufacturer") or "").strip().upper()


def _is_zwift_garmin_pair(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return {_manufacturer(left), _manufacturer(right)} == {"GARMIN", "ZWIFT"}


def _overlap_facts(
    left: Dict[str, Any], right: Dict[str, Any]
) -> Optional[Tuple[float, float, float]]:
    """Return start gap, relative duration delta, and shorter-interval overlap."""
    left_interval = _interval(left)
    right_interval = _interval(right)
    if left_interval is None or right_interval is None:
        return None

    left_start, left_end, left_duration = left_interval
    right_start, right_end, right_duration = right_interval
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    overlap_ratio = overlap / min(left_duration, right_duration)
    duration_delta = abs(left_duration - right_duration) / max(
        left_duration, right_duration
    )
    return abs(left_start - right_start), duration_delta, overlap_ratio


def _duplicate_relation(
    left: Dict[str, Any], right: Dict[str, Any]
) -> Tuple[bool, bool]:
    """Return ``(merge, ambiguous)`` for a conservative Zwift/Garmin pair."""
    if _sport_family(left) != _sport_family(right):
        return False, False
    if not _is_zwift_garmin_pair(left, right):
        return False, False

    facts = _overlap_facts(left, right)
    if facts is None:
        return False, False
    start_gap, duration_delta, overlap_ratio = facts
    same_session_shape = (
        start_gap <= ZWIFT_GARMIN_MAX_START_GAP_SECONDS
        and overlap_ratio >= ZWIFT_GARMIN_MIN_OVERLAP
    )
    if not same_session_shape:
        return False, False
    if duration_delta <= ZWIFT_GARMIN_MAX_DURATION_DELTA:
        return True, False
    return False, True


def _richness(activity: Dict[str, Any]) -> int:
    return sum(_number(activity.get(field)) is not None for field in _RECORD_FIELDS)


def _choose_representative(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Prefer Garmin for source consistency, then the richest stable record."""
    return sorted(
        group,
        key=lambda activity: (
            -int(_manufacturer(activity) == "GARMIN"),
            -_richness(activity),
            str(activity.get("activityId") or ""),
        ),
    )[0]


def _deduplicate(
    activities: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Deduplicate exact IDs and conservative Zwift/Garmin double recordings."""
    if not activities:
        return [], {
            "method": "conservative_zwift_garmin_temporal_overlap_v1",
            "groups_merged": 0,
            "activities_removed": 0,
            "exact_id_duplicates_removed": 0,
            "ambiguous_pairs_kept": 0,
        }

    # First collapse identical IDs, which can occur if an upstream page overlaps.
    by_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    anonymous: List[Dict[str, Any]] = []
    for activity in activities:
        activity_id = activity.get("activityId")
        if activity_id is None:
            anonymous.append(activity)
        else:
            by_id[str(activity_id)].append(activity)

    exact_collapsed = [_choose_representative(group) for group in by_id.values()]
    exact_id_duplicates = sum(len(group) - 1 for group in by_id.values())
    candidates = exact_collapsed + anonymous

    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root != right_root:
            parent[right_root] = left_root

    ordered = sorted(
        range(len(candidates)),
        key=lambda index: (
            _timestamp_seconds(candidates[index]) is None,
            _timestamp_seconds(candidates[index]) or 0,
        ),
    )
    ambiguous_pairs = 0
    for position, left_index in enumerate(ordered):
        left_start = _timestamp_seconds(candidates[left_index])
        if left_start is None:
            continue
        for right_index in ordered[position + 1 :]:
            right_start = _timestamp_seconds(candidates[right_index])
            if right_start is None:
                continue
            if right_start - left_start > ZWIFT_GARMIN_MAX_START_GAP_SECONDS:
                break
            merge, ambiguous = _duplicate_relation(
                candidates[left_index], candidates[right_index]
            )
            if merge:
                union(left_index, right_index)
            elif ambiguous:
                ambiguous_pairs += 1

    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for index, activity in enumerate(candidates):
        groups[find(index)].append(activity)

    merged_groups = [group for group in groups.values() if len(group) > 1]
    deduplicated = [_choose_representative(group) for group in groups.values()]
    deduplicated.sort(
        key=lambda activity: _timestamp_seconds(activity) or float("-inf"),
        reverse=True,
    )

    return deduplicated, {
        "method": "conservative_zwift_garmin_temporal_overlap_v1",
        "groups_merged": len(merged_groups),
        "activities_removed": len(activities) - len(deduplicated),
        "exact_id_duplicates_removed": exact_id_duplicates,
        "ambiguous_pairs_kept": ambiguous_pairs,
        "rule": {
            "sources": ["GARMIN", "ZWIFT"],
            "same_sport_family": True,
            "max_start_gap_seconds": ZWIFT_GARMIN_MAX_START_GAP_SECONDS,
            "max_relative_duration_delta": ZWIFT_GARMIN_MAX_DURATION_DELTA,
            "min_shorter_interval_overlap": ZWIFT_GARMIN_MIN_OVERLAP,
            "distance_similarity_required": False,
        },
        "representative_policy": "Prefer GARMIN, then the activity with more record fields.",
    }


def _fetch_all_activities(
    client, *, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES
) -> Tuple[List[Dict[str, Any]], int]:
    """Read the complete activity history with a finite no-progress guard."""
    activities: List[Dict[str, Any]] = []
    start = 0
    for page_number in range(max_pages):
        page = client.get_activities(start, page_size) or []
        if not isinstance(page, list):
            raise RuntimeError("Garmin get_activities returned a non-list response")
        if not page:
            return activities, page_number + 1
        activities.extend(page)
        if len(page) < page_size:
            return activities, page_number + 1
        start += len(page)
    raise RuntimeError(
        f"Activity pagination exceeded the safety limit of {max_pages} pages"
    )


def _activity_reference(activity: Dict[str, Any]) -> Dict[str, Any]:
    reference = {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "type": _activity_type(activity) or None,
        "start_time": activity.get("startTimeLocal") or activity.get("startTimeGMT"),
        "manufacturer": activity.get("manufacturer"),
        "distance_meters": _number(activity.get("distance")),
    }
    return {key: value for key, value in reference.items() if value is not None}


def _formatted_duration(seconds: float) -> str:
    total_milliseconds = round(seconds * 1_000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    base = f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"
    return f"{base}.{milliseconds:03d}" if milliseconds else base


def _best_record(
    activities: Iterable[Dict[str, Any]],
    field: str,
    *,
    prefer: str,
    require_within_duration: bool = False,
) -> Optional[Dict[str, Any]]:
    candidates = []
    for activity in activities:
        value = _number(activity.get(field), positive=True)
        if value is None:
            continue
        duration = _number(activity.get("duration"), positive=True)
        if require_within_duration and (duration is None or value > duration):
            continue
        candidates.append((value, activity))
    if not candidates:
        return None
    selector = min if prefer == "min" else max
    value, activity = selector(candidates, key=lambda item: item[0])
    return {
        "value": round(value, 3),
        "activity": _activity_reference(activity),
    }


def _running_records(activities: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    runs = [activity for activity in activities if _sport_family(activity) == "running"]
    best_efforts: Dict[str, Any] = {}
    coverage: Dict[str, Dict[str, int]] = {}

    for label, field, distance_meters in RUN_SPLIT_FIELDS:
        present = sum(_number(activity.get(field), positive=True) is not None for activity in runs)
        valid = sum(
            _number(activity.get(field), positive=True) is not None
            and _number(activity.get("duration"), positive=True) is not None
            and float(activity[field]) <= float(activity["duration"])
            for activity in runs
        )
        record = _best_record(
            runs,
            field,
            prefer="min",
            require_within_duration=True,
        )
        coverage[field] = {"present": present, "valid": valid}
        if record is None:
            best_efforts[label] = {
                "status": "unavailable",
                "distance_meters": distance_meters,
                "source_field": field,
            }
        else:
            record.update(
                {
                    "status": "available",
                    "duration_seconds": record.pop("value"),
                    "distance_meters": distance_meters,
                    "source_field": field,
                    "derivation": "minimum stored Garmin activity-summary split",
                }
            )
            record["formatted_time"] = _formatted_duration(record["duration_seconds"])
            best_efforts[label] = record

    half_candidates = []
    for activity in runs:
        distance = _number(activity.get("distance"), positive=True)
        duration = _number(activity.get("duration"), positive=True)
        if distance is None or duration is None:
            continue
        if HALF_MARATHON_METERS <= distance <= (
            HALF_MARATHON_METERS * (1 + HALF_MARATHON_MAX_EXTRA_DISTANCE)
        ):
            half_candidates.append((duration, activity))

    if half_candidates:
        duration, activity = min(half_candidates, key=lambda item: item[0])
        best_efforts["half_marathon"] = {
            "status": "available_as_activity_total",
            "duration_seconds": round(duration, 3),
            "formatted_time": _formatted_duration(duration),
            "distance_meters": HALF_MARATHON_METERS,
            "recorded_activity_distance_meters": round(float(activity["distance"]), 3),
            "derivation": "full activity duration for a run from target distance to +1%",
            "exact_rolling_effort": False,
            "activity": _activity_reference(activity),
        }
    else:
        best_efforts["half_marathon"] = {
            "status": "unavailable",
            "distance_meters": HALF_MARATHON_METERS,
            "reason": (
                "No stored half-marathon split field and no run from the target "
                "distance to +1%; no time was estimated."
            ),
        }

    longest_run = _best_record(runs, "distance", prefer="max")
    if longest_run is not None:
        longest_run = {
            "distance_meters": longest_run["value"],
            "activity": longest_run["activity"],
        }

    return {
        "activities_considered": len(runs),
        "best_efforts": best_efforts,
        "longest_running_activity": longest_run,
        "field_coverage": coverage,
    }


def _cycling_power_records(activities: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rides = [activity for activity in activities if _sport_family(activity) == "cycling"]
    eligible = [
        activity
        for activity in rides
        if activity.get("excludeFromPowerCurveReports") is not True
    ]
    excluded = len(rides) - len(eligible)

    instantaneous = _best_record(eligible, "maxPower", prefer="max")
    max_power = None
    if instantaneous is not None:
        max_power = {
            "watts": instantaneous["value"],
            "source_field": "maxPower",
            "activity": instantaneous["activity"],
        }

    peaks: Dict[str, Any] = {}
    coverage: Dict[str, int] = {}
    for duration_seconds, field in POWER_DURATION_FIELDS:
        coverage[field] = sum(
            _number(activity.get(field), positive=True) is not None
            for activity in eligible
        )
        record = _best_record(eligible, field, prefer="max")
        if record is None:
            continue
        peaks[str(duration_seconds)] = {
            "duration_seconds": duration_seconds,
            "watts": record["value"],
            "source_field": field,
            "derivation": "maximum stored Garmin activity-summary power peak",
            "activity": record["activity"],
        }

    return {
        "activities_considered": len(rides),
        "activities_eligible_for_power_curve": len(eligible),
        "activities_excluded_by_garmin_flag": excluded,
        "maximum_power": max_power,
        "stored_duration_peaks": peaks,
        "field_coverage": coverage,
    }


def _history_range(activities: Sequence[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    values = [
        str(activity.get("startTimeLocal") or activity.get("startTimeGMT"))
        for activity in activities
        if activity.get("startTimeLocal") or activity.get("startTimeGMT")
    ]
    return {
        "earliest_start": min(values) if values else None,
        "latest_start": max(values) if values else None,
    }


def _build_all_time_records(
    raw_activities: Sequence[Dict[str, Any]], *, pages_fetched: int
) -> Dict[str, Any]:
    activities, deduplication = _deduplicate(raw_activities)
    longest = _best_record(activities, "distance", prefer="max")
    maximum_hr = _best_record(activities, "maxHR", prefer="max")

    longest_activity = None
    if longest is not None:
        longest_activity = {
            "distance_meters": longest["value"],
            "activity": longest["activity"],
        }
    max_hr = None
    if maximum_hr is not None:
        max_hr = {
            "bpm": maximum_hr["value"],
            "activity": maximum_hr["activity"],
        }

    return {
        "status": "complete",
        "history": {
            "pages_fetched": pages_fetched,
            "activities_fetched": len(raw_activities),
            "activities_after_deduplication": len(activities),
            **_history_range(activities),
        },
        "deduplication": deduplication,
        "records": {
            "longest_activity": longest_activity,
            "maximum_heart_rate": max_hr,
            "running": _running_records(activities),
            "cycling_power": _cycling_power_records(activities),
        },
        "limitations": [
            "This is a recalculation from activity summaries, not Garmin's official Personal Records page.",
            "Only record fields stored by Garmin are used; missing values are reported as unavailable rather than estimated.",
            "Power peaks use stored maxAvgPower fields and do not download FIT files or calculate arbitrary durations.",
            "Zwift/Garmin deduplication is conservative; ambiguous pairs are retained and counted.",
        ],
    }


def register_tools(app):
    """Register the all-time records read tool."""

    @app.tool()
    async def get_all_time_records() -> str:
        """Recalculate deduplicated all-time records from the full activity history.

        Reads every Garmin activity page, removes conservative Zwift/Garmin
        double recordings, and returns stored running split bests (1 km, 5 km,
        10 km), a half-marathon result only when supported by a near-distance
        activity, longest distance, maximum heart rate, and all stored cycling
        power-duration peaks. The response includes field coverage, the exact
        deduplication rule, and explicit limitations.

        This tool is read-only. It does not edit Garmin data, scrape Garmin's
        Personal Records page, save FIT files, or invent values for missing data.
        """
        try:
            activities, pages_fetched = _fetch_all_activities(garmin_client)
            return json.dumps(
                _build_all_time_records(
                    activities,
                    pages_fetched=pages_fetched,
                ),
                indent=2,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Error retrieving all-time records: {exc}",
                },
                indent=2,
            )

    return app
