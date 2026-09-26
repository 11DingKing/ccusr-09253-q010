"""规则配置、学生进度与批量预警 API 测试。"""

from __future__ import annotations

PLAN = {
    "plan_version": "P-RULE-2024",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 10800,
}

RULE_BODY = {
    "categories": [
        {"code": "on_campus", "min_ratio": 0.4, "max_ratio": 0.7},
        {"code": "enterprise", "min_ratio": 0.2, "max_ratio": 0.5},
        {"code": "public_welfare", "min_ratio": 0, "max_ratio": 0.2},
    ],
    "activity_category_map": {
        "regular": "on_campus",
        "internship": "enterprise",
        "volunteer": "public_welfare",
    },
}


def _create_plan(client, plan=PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text


def _put_rule(client, rule_version="R1", body=None):
    return client.put(
        f"/api/plans/{PLAN['plan_version']}/rules/{rule_version}",
        json=RULE_BODY if body is None else body,
    )


def _publish(client, rule_version="R1"):
    return client.post(
        f"/api/plans/{PLAN['plan_version']}/rules/{rule_version}/publish"
    )


def _checkin(eid, student, start, end, activity_type="regular", windows=None):
    payload = {
        "activity_id": "A1",
        "activity_type": activity_type,
        "check_in_at": start,
        "check_out_at": end,
    }
    if windows is not None:
        payload["category_windows"] = windows
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": payload,
    }


def _import(client, events):
    resp = client.post(
        f"/api/plans/{PLAN['plan_version']}/events", json={"events": events}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _progress(client, student):
    resp = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/{student}/progress"
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_code(progress):
    return {c["category"]: c for c in progress["categories"]}


# ---------------------------------------------------------------- 规则配置


def test_rule_config_create_publish_flow(client):
    _create_plan(client)
    resp = _put_rule(client, "R1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["published_at"] is None
    # 占比按方案总学时精确换算：10800 * 0.4 = 4320，10800 * 0.7 = 7560。
    on_campus = body["categories"][0]
    assert on_campus["min_seconds"] == 4320
    assert on_campus["max_seconds"] == 7560

    # 未发布前没有现行规则。
    assert client.get(f"/api/plans/{PLAN['plan_version']}/rules/current").status_code == 404

    published = _publish(client, "R1")
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["published_at"] is not None

    current = client.get(f"/api/plans/{PLAN['plan_version']}/rules/current").json()
    assert current["rule_version"] == "R1"

    # 已发布的规则不可再修改。
    assert _put_rule(client, "R1").status_code == 409

    # 发布 R2 后 R1 自动退休，现行规则切换。
    r2_body = dict(RULE_BODY)
    _put_rule(client, "R2", r2_body)
    assert _publish(client, "R2").status_code == 200
    rules = client.get(f"/api/plans/{PLAN['plan_version']}/rules").json()
    by_version = {r["rule_version"]: r["status"] for r in rules}
    assert by_version == {"R1": "retired", "R2": "published"}
    current = client.get(f"/api/plans/{PLAN['plan_version']}/rules/current").json()
    assert current["rule_version"] == "R2"

    # 重复发布现行规则是幂等的。
    assert _publish(client, "R2").status_code == 200
    # 发布不存在的规则版本返回 404。
    assert _publish(client, "NOPE").status_code == 404


def test_rule_config_requires_existing_plan(client):
    resp = client.put("/api/plans/NOPE/rules/R1", json=RULE_BODY)
    assert resp.status_code == 404
    assert client.get("/api/plans/NOPE/rules").status_code == 404


def test_rule_config_ratio_rounding_in_response(client):
    _create_plan(client)
    body = {
        "categories": [
            {"code": "on_campus", "min_ratio": "0.3333", "max_ratio": "0.3333"},
            {"code": "enterprise", "min_ratio": 0, "max_ratio": 1},
        ],
        "activity_category_map": {"regular": "on_campus"},
    }
    resp = _put_rule(client, "R1", body)
    assert resp.status_code == 201, resp.text
    on_campus = resp.json()["categories"][0]
    # 10800 * 0.3333 = 3599.64：下限向上取整为 3600，上限向下取整为 3599。
    assert on_campus["min_seconds"] == 3600
    assert on_campus["max_seconds"] == 3599


def test_rule_config_validation_errors(client):
    _create_plan(client)
    # 单类别下限高于上限。
    bad_min_max = {
        "categories": [{"code": "a", "min_ratio": 0.5, "max_ratio": 0.4}],
    }
    assert _put_rule(client, "R1", bad_min_max).status_code == 422
    # 最低占比之和超过 1。
    bad_sum = {
        "categories": [
            {"code": "a", "min_ratio": 0.6, "max_ratio": 0.7},
            {"code": "b", "min_ratio": 0.6, "max_ratio": 0.7},
        ],
    }
    assert _put_rule(client, "R2", bad_sum).status_code == 422
    # 最高占比之和小于 1。
    bad_cap = {
        "categories": [
            {"code": "a", "min_ratio": 0.1, "max_ratio": 0.4},
            {"code": "b", "min_ratio": 0.1, "max_ratio": 0.4},
        ],
    }
    assert _put_rule(client, "R3", bad_cap).status_code == 422
    # 活动映射引用了未声明的类别。
    bad_map = {
        "categories": [{"code": "a", "min_ratio": 0, "max_ratio": 1}],
        "activity_category_map": {"regular": "ghost"},
    }
    assert _put_rule(client, "R4", bad_map).status_code == 422
    # 比例超出 [0, 1]。
    bad_ratio = {
        "categories": [{"code": "a", "min_ratio": 0, "max_ratio": 1.5}],
    }
    assert _put_rule(client, "R5", bad_ratio).status_code == 422
    # 类别编码重复。
    dup = {
        "categories": [
            {"code": "a", "min_ratio": 0, "max_ratio": 0.6},
            {"code": "a", "min_ratio": 0, "max_ratio": 0.6},
        ],
    }
    assert _put_rule(client, "R6", dup).status_code == 422


# ------------------------------------------------------- 规则版本固定到事件


def test_events_are_pinned_to_rule_version_at_import(client):
    _create_plan(client)
    # 没有任何已发布规则时导入的事件钉住 None。
    result = _import(
        client,
        [_checkin("E-00", "S1", "2024-03-14T08:00:00+08:00", "2024-03-14T09:00:00+08:00")],
    )
    assert result["pinned_rule_version"] is None

    _put_rule(client, "R1")
    _publish(client, "R1")
    result = _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    assert result["pinned_rule_version"] == "R1"

    # 规则升级：R2 把 regular 映射到企业。
    r2_body = {
        "categories": RULE_BODY["categories"],
        "activity_category_map": {
            "regular": "enterprise",
            "internship": "enterprise",
            "volunteer": "public_welfare",
        },
    }
    _put_rule(client, "R2", r2_body)
    _publish(client, "R2")
    _import(
        client,
        [_checkin("E-02", "S1", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )

    progress = _progress(client, "S1")
    checkins = {c["event_id"]: c for c in progress["checkins"]}
    assert checkins["E-00"]["rule_version"] is None
    assert checkins["E-01"]["rule_version"] == "R1"
    assert checkins["E-02"]["rule_version"] == "R2"
    by_code = _by_code(progress)
    # E-01 仍按 R1 计入校内，E-02 按 R2 计入企业，E-00 无规则不可计入。
    assert by_code["on_campus"]["attributed_seconds"] == 7200
    assert by_code["enterprise"]["attributed_seconds"] == 7200
    unpinned = [e for e in progress["exclusions"] if e["reason"] == "no_rule_pinned"]
    assert len(unpinned) == 1
    assert unpinned[0]["seconds"] == 3600


def test_unpinned_events_stay_in_gross_total_but_not_counted(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    _put_rule(client, "R1")
    _publish(client, "R1")
    progress = _progress(client, "S1")
    # 毛总量仍在，但分项口径下计入量为零。
    assert progress["total_seconds"] == 7200
    assert progress["counted_seconds"] == 0
    assert progress["meets_requirement"] is False
    assert progress["total_shortfall_seconds"] == 10800


# ------------------------------------------------------- 学生进度：缺口与原因


def test_progress_reports_shortfall_cap_and_pending_reasons(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [
            # 校内 2 小时（下限 4320 秒，达标）。
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            # 企业 1 小时，等待导师确认，暂不可计入。
            _checkin(
                "E-02",
                "S1",
                "2024-03-15T10:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
                activity_type="internship",
            ),
            # 一次签到跨越两个类别授权区间，末段 1 小时无授权。
            _checkin(
                "E-03",
                "S1",
                "2024-03-16T08:00:00+08:00",
                "2024-03-16T14:00:00+08:00",
                windows=[
                    {
                        "category": "on_campus",
                        "start": "2024-03-16T08:00:00+08:00",
                        "end": "2024-03-16T10:00:00+08:00",
                    },
                    {
                        "category": "enterprise",
                        "start": "2024-03-16T10:00:00+08:00",
                        "end": "2024-03-16T13:00:00+08:00",
                    },
                ],
            ),
        ],
    )
    progress = _progress(client, "S1")
    by_code = _by_code(progress)
    # 校内：7200 + 7200 = 14400 归属，但上限 7560，超出部分不可计入。
    assert by_code["on_campus"]["attributed_seconds"] == 14400
    assert by_code["on_campus"]["counted_seconds"] == 7560
    assert by_code["on_campus"]["over_cap_seconds"] == 6840
    # 企业：授权区间 3 小时计入，下限 2160 达标。
    assert by_code["enterprise"]["attributed_seconds"] == 3 * 3600
    assert by_code["enterprise"]["shortfall_seconds"] == 0
    # 计入总量 = 7560 + 5400（企业上限）= 12960，总缺口为 0。
    assert progress["counted_seconds"] == 7560 + 5400
    assert progress["total_shortfall_seconds"] == 0
    assert progress["meets_requirement"] is True

    reasons = {(e["reason"], e.get("event_id")): e["seconds"] for e in progress["exclusions"]}
    assert reasons[("no_authorization_window", "E-03")] == 3600
    assert reasons[("pending_mentor_confirmation", "E-02")] == 3600
    assert reasons[("category_cap_exceeded", None)] == 6840


def test_negative_correction_targets_category_and_clamps(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            {
                "event_id": "E-02",
                "event_type": "leave_correction",
                "student_id": "S1",
                "payload": {
                    "adjustment_seconds": -1800,
                    "reason": "迟到扣减",
                    "category": "on_campus",
                },
            },
            {
                "event_id": "E-03",
                "event_type": "leave_correction",
                "student_id": "S1",
                "payload": {
                    "adjustment_seconds": -99999,
                    "reason": "超出归属的负向修正",
                    "category": "on_campus",
                },
            },
        ],
    )
    progress = _progress(client, "S1")
    on_campus = _by_code(progress)["on_campus"]
    assert on_campus["attributed_seconds"] == 7200
    assert on_campus["adjustment_seconds"] == -1800 - 99999
    # 类别计入量钳制到 0，不为负。
    assert on_campus["counted_seconds"] == 0
    assert on_campus["shortfall_seconds"] == 4320
    assert progress["counted_seconds"] == 0
    assert progress["meets_requirement"] is False


def test_cross_day_checkin_reports_category_split_via_api(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T22:00:00+08:00",
                "2024-03-16T02:00:00+08:00",
                windows=[
                    {
                        "category": "on_campus",
                        "start": "2024-03-15T22:00:00+08:00",
                        "end": "2024-03-15T23:30:00+08:00",
                    },
                    {
                        "category": "enterprise",
                        "start": "2024-03-15T23:30:00+08:00",
                        "end": "2024-03-16T02:00:00+08:00",
                    },
                ],
            )
        ],
    )
    progress = _progress(client, "S1")
    daily = {d["academic_day"]: d["seconds"] for d in progress["daily"]}
    assert daily == {"2024-03-15": 7200, "2024-03-16": 7200}
    daily_categories = {
        (d["academic_day"], d["category"]): d["seconds"]
        for d in progress["daily_categories"]
    }
    assert daily_categories == {
        ("2024-03-15", "on_campus"): 5400,
        ("2024-03-15", "enterprise"): 1800,
        ("2024-03-16", "enterprise"): 7200,
    }


# ------------------------------------------------------------------ 批量预警


def test_batch_warnings_flag_at_risk_students(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [
            # S1：只有 1 小时校内，总量与各类别均不达标。
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            # S2：校内 2 小时 + 企业 1 小时（已确认），总计 3 小时全部达标。
            _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin(
                "E-03",
                "S2",
                "2024-03-15T10:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
                activity_type="internship",
            ),
            {
                "event_id": "E-04",
                "event_type": "mentor_confirm",
                "student_id": "S2",
                "payload": {"checkin_event_id": "E-03"},
            },
            # S3：有时长等待导师确认，属预警但不属严重缺口。
            _checkin(
                "E-05",
                "S3",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
                activity_type="internship",
            ),
        ],
    )
    resp = client.get(f"/api/plans/{PLAN['plan_version']}/warnings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule_version"] == "R1"
    assert body["required_seconds"] == 10800

    by_id = {s["student_id"]: s for s in body["students"]}
    assert by_id["S1"]["severity"] == "critical"
    codes_s1 = {w["code"] for w in by_id["S1"]["warnings"]}
    assert "total_shortfall" in codes_s1
    assert "category_shortfall" in codes_s1

    assert by_id["S2"]["severity"] == "ok"
    assert by_id["S2"]["warnings"] == []
    assert by_id["S2"]["meets_requirement"] is True

    assert by_id["S3"]["severity"] == "critical"  # 待确认时长导致总量缺口
    codes_s3 = {w["code"] for w in by_id["S3"]["warnings"]}
    assert "pending_confirmation" in codes_s3

    assert body["students_at_risk"] == 2

    filtered = client.get(
        f"/api/plans/{PLAN['plan_version']}/warnings", params={"at_risk_only": True}
    ).json()
    assert {s["student_id"] for s in filtered["students"]} == {"S1", "S3"}
    assert filtered["students_at_risk"] == 2


def test_warnings_require_existing_plan(client):
    assert client.get("/api/plans/NOPE/warnings").status_code == 404


def test_batch_warnings_work_without_any_rule(client):
    # 未发布规则的方案沿用旧版总量语义，预警仍可用。
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin(
                "E-02",
                "S2",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
            ),
        ],
    )
    body = client.get(f"/api/plans/{PLAN['plan_version']}/warnings").json()
    assert body["rule_version"] is None
    by_id = {s["student_id"]: s for s in body["students"]}
    assert by_id["S1"]["severity"] == "critical"
    assert {w["code"] for w in by_id["S1"]["warnings"]} == {"total_shortfall"}
    assert by_id["S2"]["severity"] == "ok"
    assert by_id["S2"]["meets_requirement"] is True


# ------------------------------------------------------- 冻结快照的分项依据


def test_freeze_snapshot_preserves_category_basis(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T14:00:00+08:00",
                windows=[
                    {
                        "category": "on_campus",
                        "start": "2024-03-15T08:00:00+08:00",
                        "end": "2024-03-15T10:00:00+08:00",
                    },
                    {
                        "category": "enterprise",
                        "start": "2024-03-15T10:00:00+08:00",
                        "end": "2024-03-15T13:00:00+08:00",
                    },
                ],
            )
        ],
    )
    f1 = client.post(f"/api/plans/{PLAN['plan_version']}/freezes/F-01", json={})
    assert f1.status_code == 201, f1.text
    body = f1.json()
    # 冻结快照记录判定所用的规则版本。
    assert body["rule_version"] == "R1"
    student = body["students"][0]
    by_code = {c["category"]: c for c in student["categories"]}
    assert by_code["on_campus"]["counted_seconds"] == 7200
    assert by_code["enterprise"]["counted_seconds"] == 5400  # 上限 10800*0.5
    assert student["counted_seconds"] == 7200 + 5400
    # 各分项依据：单次签到的授权区间片段与不可计入片段。
    checkin = student["checkins"][0]
    assert checkin["rule_version"] == "R1"
    assert [s["category"] for s in checkin["category_segments"]] == [
        "on_campus",
        "enterprise",
    ]
    assert checkin["excluded_segments"][0]["reason"] == "no_authorization_window"
    assert student["exclusions"]

    # 冻结后到达的事件不改变 F-01。
    _import(
        client,
        [_checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00")],
    )
    again = client.get(f"/api/plans/{PLAN['plan_version']}/freezes/F-01").json()
    assert again["students"][0]["counted_seconds"] == 7200 + 5400
    assert len(again["students"][0]["checkins"]) == 1

    # 新冻结反映新事件，差异查询能看到计入量变化。
    # E-02 使校内归属达到 14400，超出上限 7560 的部分被封顶。
    f2 = client.post(f"/api/plans/{PLAN['plan_version']}/freezes/F-02", json={}).json()
    assert f2["students"][0]["counted_seconds"] == 7560 + 5400
    diff = client.get(f"/api/plans/{PLAN['plan_version']}/freezes/F-01/diff/F-02").json()
    assert diff["old_rule_version"] == "R1"
    assert diff["new_rule_version"] == "R1"
    change = diff["student_changes"][0]
    assert change["fields"]["counted_seconds"] == {"before": 12600, "after": 12960}


def test_frozen_explain_shows_category_breakdown(client):
    _create_plan(client)
    _put_rule(client, "R1")
    _publish(client, "R1")
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    client.post(f"/api/plans/{PLAN['plan_version']}/freezes/F-01", json={})
    explanation = client.get(
        f"/api/plans/{PLAN['plan_version']}/freezes/F-01/explain/S1"
    ).json()
    by_code = {c["category"]: c for c in explanation["categories"]}
    assert by_code["on_campus"]["attributed_seconds"] == 7200
    assert by_code["on_campus"]["min_seconds"] == 4320
    assert by_code["on_campus"]["meets_minimum"] is True
    assert by_code["enterprise"]["shortfall_seconds"] == 2160
    assert explanation["checkins"][0]["category_segments"][0]["source"] == (
        "activity_type_mapping"
    )
