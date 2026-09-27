import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class TransitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_route_version_review_publish_and_snapshot_isolation(self):
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"])["minutes"], 23)
        disruption = self.db.create_disruption("planner-01", {"code": "D-001", "name": "会展站跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.transition(v1, "planner-01", "planner", "publish")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)

        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)
        # Publishing v2 as a draft snapshot does not alter the old published v1.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)

    def test_cross_midnight_times_and_bad_data_isolation(self):
        times = self.db.trip_times(self.db.list_trips()[0]["id"])
        self.assertEqual(times[0]["clock"], "23:50")
        self.assertEqual(times[-1]["service_minute"], 1461)
        self.assertEqual(times[-1]["clock"], "00:21")
        self.assertEqual(times[-1]["day_offset"], 1)

        fresh = Database(Path(self.tmp.name) / "bad.db")
        result = fresh.import_base("planner-01", {
            "lines": [{"code": "B1", "name": "错误线路"}],
            "stops": [{"code": "B-S1", "name": "站点一", "latitude": 31, "longitude": 121}],
            "line_stops": [
                {"line_code": "B1", "stop_code": "B-S1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "B1", "stop_code": "NO-SUCH", "sequence": 1, "travel_minutes_from_previous": 5},
            ],
            "trips": [],
        }, "planner")
        self.assertFalse(result["accepted"])
        self.assertTrue(fresh.list_import_errors())
        self.assertEqual(fresh.list_lines(), [])

    def test_accessibility_and_conflict_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-002", "name": "站点无障碍设施故障", "starts_at": "2026-09-24T00:00:00+08:00", "ends_at": "2026-09-25T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        normal = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=False)
        accessible = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=True)
        self.assertEqual(normal["minutes"], 23)
        self.assertEqual(accessible["minutes"], 31)
        with self.assertRaises(DomainError):
            self.db.add_change(version, "viewer", {"kind": "stop_closure", "stop_id": self.stops["S2"]}, "viewer")

    def test_stop_relocation_walk_and_window_restore(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-003", "name": "人民广场站台施工", "starts_at": "2026-09-27T08:00:00+08:00", "ends_at": "2026-09-27T20:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {
            "kind": "stop_relocation",
            "stop_id": self.stops["S2"],
            "to_stop_id": self.stops["T2"],
            "travel_minutes": 6,
            "effective_start_minute": 480,
            "effective_end_minute": 1200,
        }, "planner")

        # Outside the window the original stop is served as before.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], version, at_minute=400)["minutes"], 23)

        # Inside the window a trip ending at the original stop rides to the
        # temporary platform and walks back; the ride itself is unchanged.
        inside = self.db.route(self.stops["S1"], self.stops["S2"], version, at_minute=600)
        self.assertEqual(inside["minutes"], 16)
        kinds = [leg["kind"] for leg in inside["legs"]]
        self.assertEqual(kinds, ["route", "walk"])
        walk = inside["legs"][-1]
        self.assertEqual(walk["original_stop_id"], self.stops["S2"])
        self.assertEqual(walk["temporary_stop_id"], self.stops["T2"])
        # Reverse direction walks first, then rides on.
        reverse = self.db.route(self.stops["S2"], self.stops["S3"], version, at_minute=600)
        self.assertEqual(reverse["minutes"], 11)
        self.assertEqual([leg["kind"] for leg in reverse["legs"]], ["walk", "route"])
        # Through trips keep the riding minutes and never walk.
        through = self.db.route(self.stops["S1"], self.stops["S5"], version, at_minute=600)
        self.assertEqual(through["minutes"], 23)
        self.assertTrue(all(leg["kind"] == "route" for leg in through["legs"]))
        # Past the window end the original platform is restored.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S2"], version, at_minute=1300)["minutes"], 10)

    def test_relocation_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-004", "name": "迁移校验", "starts_at": "2026-09-27T08:00:00+08:00", "ends_at": "2026-09-27T20:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        bad = [
            {"kind": "stop_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["S2"], "travel_minutes": 5},
            {"kind": "stop_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["T2"], "travel_minutes": -1},
            {"kind": "stop_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["T2"], "travel_minutes": 6, "effective_start_minute": 1200, "effective_end_minute": 480},
        ]
        for payload in bad:
            with self.assertRaises(DomainError):
                self.db.add_change(version, "planner-01", payload, "planner")

    def test_relocation_without_accessible_platform_fails_over(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-005", "name": "巷内临停", "starts_at": "2026-09-27T08:00:00+08:00", "ends_at": "2026-09-27T20:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {
            "kind": "stop_relocation",
            "stop_id": self.stops["S2"],
            "to_stop_id": self.stops["T7"],
            "travel_minutes": 8,
            "effective_start_minute": 480,
            "effective_end_minute": 1200,
        }, "planner")
        # Ordinary passengers use the lane-side temporary platform (+8 walk).
        normal = self.db.route(self.stops["S1"], self.stops["S2"], version, at_minute=600)
        self.assertEqual(normal["status"], "ok")
        self.assertEqual(normal["minutes"], 18)
        # Accessible routing must not send wheelchair users to a platform
        # without step-free access: no usable route to the relocated stop.
        blocked = self.db.route(self.stops["S1"], self.stops["S2"], version, at_minute=600, require_accessible=True)
        self.assertEqual(blocked["status"], "unreachable")
        # But through trips still have a walkable route: the vehicle passes
        # the relocated stop without anyone boarding/alighting there.
        ride_through = self.db.route(self.stops["S1"], self.stops["S5"], version, at_minute=600, require_accessible=True)
        self.assertEqual(ride_through["status"], "ok")
        self.assertEqual(ride_through["minutes"], 23)
        # And the original stop works again once the window passes.
        restored = self.db.route(self.stops["S1"], self.stops["S2"], version, at_minute=1300, require_accessible=True)
        self.assertEqual(restored["status"], "ok")
        self.assertEqual(restored["minutes"], 10)

    def test_published_old_version_keeps_original_serving_after_relocation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-006", "name": "旧版发布隔离", "starts_at": "2026-09-27T08:00:00+08:00", "ends_at": "2026-09-30T20:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {
            "kind": "stop_relocation",
            "stop_id": self.stops["S2"],
            "to_stop_id": self.stops["T2"],
            "travel_minutes": 6,
            "effective_start_minute": 480,
            "effective_end_minute": 1200,
        }, "planner")
        # The already-published old version keeps the original stopping pattern.
        old = self.db.route(self.stops["S1"], self.stops["S2"], v1, at_minute=600)
        self.assertEqual(old["minutes"], 10)
        new = self.db.route(self.stops["S1"], self.stops["S2"], v2, at_minute=600)
        self.assertEqual(new["minutes"], 16)


if __name__ == "__main__":
    unittest.main()
