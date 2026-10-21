"""Tests for the deduplicated all-time Garmin records tool."""

import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import all_time_records
from garmin_mcp.all_time_records import (
    _build_all_time_records,
    _deduplicate,
    _fetch_all_activities,
)


def _activity(
    activity_id,
    *,
    activity_type="running",
    manufacturer="GARMIN",
    start="2026-01-01 10:00:00",
    duration=1_800.0,
    distance=5_000.0,
    **metrics,
):
    return {
        "activityId": activity_id,
        "activityName": f"Activity {activity_id}",
        "activityType": {"typeKey": activity_type},
        "manufacturer": manufacturer,
        "startTimeLocal": start,
        "duration": duration,
        "distance": distance,
        **metrics,
    }


def test_fetch_all_activities_walks_every_page():
    client = Mock()
    first = [_activity(1), _activity(2)]
    second = [_activity(3)]
    client.get_activities.side_effect = [first, second]

    activities, pages = _fetch_all_activities(client, page_size=2)

    assert [activity["activityId"] for activity in activities] == [1, 2, 3]
    assert pages == 2
    assert client.get_activities.call_args_list == [
        ((0, 2),),
        ((2, 2),),
    ]


def test_deduplicate_merges_zwift_garmin_overlap_without_distance_match():
    garmin = _activity(
        1,
        activity_type="indoor_cycling",
        manufacturer="GARMIN",
        duration=3_600,
        distance=40_000,
        maxAvgPower_300=250,
    )
    zwift = _activity(
        2,
        activity_type="virtual_ride",
        manufacturer="ZWIFT",
        start="2026-01-01 10:02:00",
        duration=3_700,
        distance=25_000,
        maxAvgPower_300=260,
    )

    deduplicated, stats = _deduplicate([zwift, garmin])

    assert [activity["activityId"] for activity in deduplicated] == [1]
    assert stats["groups_merged"] == 1
    assert stats["activities_removed"] == 1
    assert stats["rule"]["distance_similarity_required"] is False


@pytest.mark.parametrize(
    "other, expected_ambiguous",
    [
        (
            _activity(
                2,
                activity_type="indoor_cycling",
                manufacturer="GARMIN",
                start="2026-01-01 10:01:00",
            ),
            0,
        ),
        (
            _activity(
                2,
                activity_type="running",
                manufacturer="ZWIFT",
                start="2026-01-01 10:01:00",
            ),
            0,
        ),
        (
            _activity(
                2,
                activity_type="virtual_ride",
                manufacturer="ZWIFT",
                start="2026-01-01 10:01:00",
                duration=300,
            ),
            1,
        ),
    ],
)
def test_deduplicate_keeps_non_duplicates(other, expected_ambiguous):
    garmin = _activity(
        1,
        activity_type="indoor_cycling",
        manufacturer="GARMIN",
        duration=1_800,
    )

    deduplicated, stats = _deduplicate([garmin, other])

    assert len(deduplicated) == 2
    assert stats["groups_merged"] == 0
    assert stats["ambiguous_pairs_kept"] == expected_ambiguous


def test_build_records_uses_stored_values_and_honors_power_exclusion():
    activities = [
        _activity(
            1,
            duration=1_600,
            distance=5_100,
            fastestSplit_1000=300,
            fastestSplit_5000=1_500,
            maxHR=180,
        ),
        _activity(
            2,
            start="2026-01-02 10:00:00",
            duration=2_400,
            distance=10_000,
            fastestSplit_1000=250,
            fastestSplit_5000=1_400,
            fastestSplit_10000=2_400,
            maxHR=175,
        ),
        _activity(
            3,
            start="2026-01-03 10:00:00",
            duration=1_000,
            distance=10_000,
            fastestSplit_10000=2_000,
            maxHR=170,
        ),
        _activity(
            4,
            activity_type="cycling",
            start="2026-01-04 10:00:00",
            duration=3_600,
            distance=45_000,
            maxHR=165,
            maxPower=900,
            maxAvgPower_30=600,
            maxAvgPower_300=350,
            maxAvgPower_1200=280,
            excludeFromPowerCurveReports=False,
        ),
        _activity(
            5,
            activity_type="cycling",
            start="2026-01-05 10:00:00",
            duration=3_600,
            distance=40_000,
            maxPower=1_200,
            maxAvgPower_30=900,
            maxAvgPower_300=700,
            maxAvgPower_1200=500,
            excludeFromPowerCurveReports=True,
        ),
        _activity(
            6,
            activity_type="open_water_swimming",
            start="2026-01-06 10:00:00",
            duration=4_000,
            distance=50_000,
            maxHR=190,
        ),
    ]

    result = _build_all_time_records(activities, pages_fetched=1)
    records = result["records"]

    assert result["status"] == "complete"
    assert records["longest_activity"]["distance_meters"] == 50_000
    assert records["maximum_heart_rate"]["bpm"] == 190
    assert (
        records["running"]["best_efforts"]["one_kilometer"]["duration_seconds"]
        == 250
    )
    assert (
        records["running"]["best_efforts"]["ten_kilometers"]["duration_seconds"]
        == 2_400
    )
    assert records["running"]["field_coverage"]["fastestSplit_10000"] == {
        "present": 2,
        "valid": 1,
    }
    assert records["running"]["best_efforts"]["half_marathon"]["status"] == "unavailable"
    power = records["cycling_power"]
    assert power["activities_excluded_by_garmin_flag"] == 1
    assert power["maximum_power"]["watts"] == 900
    assert power["stored_duration_peaks"]["30"]["watts"] == 600
    assert power["stored_duration_peaks"]["300"]["watts"] == 350
    assert power["stored_duration_peaks"]["1200"]["watts"] == 280


def test_half_marathon_is_only_reported_as_near_distance_activity_total():
    run = _activity(
        1,
        duration=5_000,
        distance=21_150,
    )

    result = _build_all_time_records([run], pages_fetched=1)
    half = result["records"]["running"]["best_efforts"]["half_marathon"]

    assert half["status"] == "available_as_activity_total"
    assert half["exact_rolling_effort"] is False
    assert half["recorded_activity_distance_meters"] == 21_150


@pytest.mark.asyncio
async def test_get_all_time_records_tool_returns_json(mock_garmin_client):
    mock_garmin_client.get_activities.return_value = [
        _activity(1, fastestSplit_1000=300, maxHR=180)
    ]
    all_time_records.configure(mock_garmin_client)
    app = FastMCP("Test All-Time Records")
    app = all_time_records.register_tools(app)

    result = await app.call_tool("get_all_time_records", {})
    data = json.loads(result[0][0].text)

    assert data["status"] == "complete"
    assert data["history"]["activities_fetched"] == 1
    assert data["records"]["running"]["best_efforts"]["one_kilometer"]["status"] == "available"
    mock_garmin_client.get_activities.assert_called_once_with(0, 100)
