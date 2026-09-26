"""服务端业务模块。"""

from __future__ import annotations

PLAN = {
    "plan_version": "P-RULES",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 10800,
}

RV1_DEFINITION = {
    "categories": [
        {"key": "on_campus", "min_permille": 500, "max_permille": 1000},
        {"key": "enterprise", "min_permille": 200, "max_permille": 1000},
    ],
    "authorization_windows": [
        {
            "category": "on_campus",
            "start": "2024-03-01T00:00:00Z",
            "end": "2024-04-01T00:00:00Z",
        },
        {
            "category": "enterprise",
            "start": "2024-04-01T00:00:00Z",
            "end": "2024-05-01T00:00:00Z",
        },
    ],
    "activity_category_map": {
        "regular": "on_campus",
        "internship": "enterprise",
    },
    "default_category": None,
}


def _create_plan(client, plan=None):
    resp = client.post("/api/plans", json=plan or PLAN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _import(client, events, plan_version=PLAN["plan_version"]):
    resp = client.post(f"/api/plans/{plan_version}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _progress(client, student, plan_version=PLAN["plan_version"]):
    resp = client.get(f"/api/plans/{plan_version}/students/{student}/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_plan_creation_registers_default_rule(client):
    plan = _create_plan(client)
    assert plan["active_rule_version"] == "rv-default"

    listing = client.get(f"/api/plans/{PLAN['plan_version']}/rules").json()
    assert listing["active_rule_version"] == "rv-default"
    assert len(listing["rules"]) == 1
    default_rule = listing["rules"][0]
    assert default_rule["rule_version"] == "rv-default"
    assert default_rule["is_active"] is True
    assert default_rule["definition"]["categories"] == []


def test_put_rule_creates_activates_and_validates(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    resp = client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": RV1_DEFINITION, "activate": True},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_active"] is True
    assert body["definition"]["categories"][0]["key"] == "on_campus"

    fetched = client.get(f"/api/plans/{pv}/rules/rv-1").json()
    assert fetched["rule_version"] == "rv-1"
    assert fetched["is_active"] is True

    listing = client.get(f"/api/plans/{pv}/rules").json()
    assert listing["active_rule_version"] == "rv-1"
    assert {r["rule_version"] for r in listing["rules"]} == {"rv-default", "rv-1"}


def test_put_rule_rejects_invalid_definitions(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    base = {"definition": RV1_DEFINITION, "activate": False}

    too_wide = {
        "categories": [{"key": "on_campus", "min_permille": 600, "max_permille": 500}]
    }
    assert client.put(
        f"/api/plans/{pv}/rules/bad-1", json={"definition": too_wide}
    ).status_code == 422

    unknown_window = {
        "categories": [{"key": "on_campus"}],
        "authorization_windows": [
            {
                "category": "enterprise",
                "start": "2024-03-01T00:00:00Z",
                "end": "2024-04-01T00:00:00Z",
            }
        ],
    }
    assert client.put(
        f"/api/plans/{pv}/rules/bad-2", json={"definition": unknown_window}
    ).status_code == 422

    duplicated = {
        "categories": [{"key": "on_campus"}, {"key": "on_campus"}],
    }
    assert client.put(
        f"/api/plans/{pv}/rules/bad-3", json={"definition": duplicated}
    ).status_code == 422

    naive_window = {
        "categories": [{"key": "on_campus"}],
        "authorization_windows": [
            {"category": "on_campus", "start": "2024-03-01T00:00:00", "end": "2024-04-01T00:00:00"}
        ],
    }
    assert client.put(
        f"/api/plans/{pv}/rules/bad-4", json={"definition": naive_window}
    ).status_code == 422

    assert base["definition"]  # 合法定义仍可用
    assert client.put(
        f"/api/plans/{pv}/rules/rv-ok", json=base
    ).status_code == 201


def test_put_rule_is_idempotent_and_conflicts_on_different_content(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    first = client.put(
        f"/api/plans/{pv}/rules/rv-1", json={"definition": RV1_DEFINITION}
    )
    assert first.status_code == 201

    again = client.put(
        f"/api/plans/{pv}/rules/rv-1", json={"definition": RV1_DEFINITION}
    )
    assert again.status_code == 200
    assert again.json()["is_active"] is False

    changed = dict(RV1_DEFINITION)
    changed["categories"] = [
        {"key": "on_campus", "min_permille": 100, "max_permille": 1000},
        {"key": "enterprise", "min_permille": 200, "max_permille": 1000},
    ]
    conflict = client.put(
        f"/api/plans/{pv}/rules/rv-1", json={"definition": changed}
    )
    assert conflict.status_code == 409


def test_activate_unknown_rule_returns_404(client):
    _create_plan(client)
    resp = client.post(f"/api/plans/{PLAN['plan_version']}/rules/nope/activate")
    assert resp.status_code == 404


def test_legacy_events_become_uncategorized_after_rule_upgrade(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    # 规则升级前导入的事件固定在 rv-default 上。
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    before = _progress(client, "S1")
    assert before["categories"] == []
    assert before["uncategorized_seconds"] == 7200
    assert before["event_rule_versions"] == ["rv-default"]

    client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": RV1_DEFINITION, "activate": True},
    )
    after = _progress(client, "S1")
    # 旧事件保持未分类：计入总量，但不满足任何分项。
    assert after["uncategorized_seconds"] == 7200
    assert after["total_seconds"] == 7200
    assert after["event_rule_versions"] == ["rv-default"]
    by_key = {c["category"]: c for c in after["categories"]}
    assert by_key["on_campus"]["counted_seconds"] == 0
    assert by_key["on_campus"]["gap_seconds"] == 5400
    assert after["meets_requirement"] is False


def test_rule_upgrade_pins_new_events_and_snapshot_shows_basis(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": RV1_DEFINITION, "activate": True},
    )
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )

    # 升级规则：企业类别上限收紧到 500‰，其余不变。
    rv2 = dict(RV1_DEFINITION)
    rv2["categories"] = [
        {"key": "on_campus", "min_permille": 500, "max_permille": 1000},
        {"key": "enterprise", "min_permille": 200, "max_permille": 500},
    ]
    client.put(f"/api/plans/{pv}/rules/rv-2", json={"definition": rv2, "activate": True})

    _import(
        client,
        [
            _checkin(
                "E-02",
                "S1",
                "2024-04-10T08:00:00+08:00",
                "2024-04-10T12:00:00+08:00",
                activity_type="internship",
            ),
            {
                "event_id": "E-03",
                "event_type": "mentor_confirm",
                "student_id": "S1",
                "payload": {"checkin_event_id": "E-02"},
            },
        ],
    )

    progress = _progress(client, "S1")
    assert progress["event_rule_versions"] == ["rv-1", "rv-2"]
    by_key = {c["category"]: c for c in progress["categories"]}
    assert by_key["on_campus"]["counted_seconds"] == 7200
    # 企业 4 小时计入，但 rv-2 上限为 10800 * 500‰ = 5400。
    assert by_key["enterprise"]["counted_seconds"] == 14400
    assert by_key["enterprise"]["credited_seconds"] == 5400
    assert by_key["enterprise"]["excess_seconds"] == 9000
    assert {
        "reason": "exceeds_category_cap",
        "category": "enterprise",
        "seconds": 9000,
    } in progress["non_countable"]

    snapshot = client.get(f"/api/plans/{pv}/snapshot").json()
    assert snapshot["rule_version"] == "rv-2"
    assert snapshot["rule_versions"] == ["rv-1", "rv-2"]
    student = snapshot["students"][0]
    enterprise = {c["category"]: c for c in student["categories"]}["enterprise"]
    # 冻结依据：千分比与换算阈值同时落库。
    assert enterprise["max_permille"] == 500
    assert enterprise["max_seconds"] == 5400
    assert enterprise["min_permille"] == 200
    assert enterprise["min_seconds"] == 2160


def test_warnings_report_lists_students_with_gaps(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": RV1_DEFINITION, "activate": True},
    )
    # S1 只有校内学时，企业分项缺口 2160。
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    # S2 校内 2 小时 + 企业 4 小时（已确认），两个分项都达标。
    _import(
        client,
        [
            _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin(
                "E-03",
                "S2",
                "2024-04-10T08:00:00+08:00",
                "2024-04-10T12:00:00+08:00",
                activity_type="internship",
            ),
            {
                "event_id": "E-04",
                "event_type": "mentor_confirm",
                "student_id": "S2",
                "payload": {"checkin_event_id": "E-03"},
            },
        ],
    )

    warnings = client.get(f"/api/plans/{pv}/warnings").json()
    assert warnings["rule_version"] == "rv-1"
    assert warnings["students_at_risk"] == 1
    entry = warnings["warnings"][0]
    assert entry["student_id"] == "S1"
    assert entry["meets_requirement"] is False
    assert entry["total_gap_seconds"] == 10800 - 7200
    assert entry["category_gaps"] == [
        {
            "category": "enterprise",
            "min_seconds": 2160,
            "credited_seconds": 0,
            "gap_seconds": 2160,
        }
    ]

    full = client.get(f"/api/plans/{pv}/warnings?include_compliant=true").json()
    assert full["students_at_risk"] == 2
    compliant = {w["student_id"] for w in full["warnings"] if w["meets_requirement"]}
    assert compliant == {"S2"}


def test_freeze_snapshot_preserves_category_basis(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": RV1_DEFINITION, "activate": True},
    )
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    frozen = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert frozen["rule_version"] == "rv-1"
    student = frozen["students"][0]
    on_campus = {c["category"]: c for c in student["categories"]}["on_campus"]
    assert on_campus["min_permille"] == 500
    assert on_campus["min_seconds"] == 5400
    assert on_campus["credited_seconds"] == 7200
    assert on_campus["min_met"] is True

    # 冻结后到达的事件不改变快照中的分项依据。
    _import(
        client,
        [_checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00")],
    )
    again = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    again_on_campus = {
        c["category"]: c for c in again["students"][0]["categories"]
    }["on_campus"]
    assert again_on_campus["credited_seconds"] == 7200

    explanation = client.get(f"/api/plans/{pv}/freezes/F-01/explain/S1").json()
    assert explanation["event_rule_versions"] == ["rv-1"]
    assert explanation["checkins"][0]["category"] == "on_campus"
    assert explanation["checkins"][0]["rule_version"] == "rv-1"
    assert explanation["checkins"][0]["allocated_seconds"] == 7200


def test_cross_day_checkin_with_window_split_via_api(client):
    _create_plan(client)
    pv = PLAN["plan_version"]
    definition = {
        "categories": [{"key": "on_campus", "min_permille": 0}],
        "authorization_windows": [
            {
                "category": "on_campus",
                "start": "2024-03-14T16:00:00Z",  # 2024-03-15 00:00 +08
                "end": "2024-03-15T16:00:00Z",  # 2024-03-16 00:00 +08
            }
        ],
        "activity_category_map": {"regular": "on_campus"},
    }
    client.put(
        f"/api/plans/{pv}/rules/rv-1",
        json={"definition": definition, "activate": True},
    )
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00")],
    )
    progress = _progress(client, "S1")
    days = {d["academic_day"]: d for d in progress["daily"]}
    # 授权区间在午夜结束，次日凌晨部分不可计入。
    assert set(days) == {"2024-03-15"}
    assert days["2024-03-15"]["categories"] == {"on_campus": 7200}
    assert progress["categories"][0]["counted_seconds"] == 7200
    assert {
        "reason": "outside_authorization_window",
        "seconds": 7200,
        "category": None,
    } in progress["non_countable"]
