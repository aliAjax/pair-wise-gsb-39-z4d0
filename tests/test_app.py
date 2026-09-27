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

    def _relocation_version(self, code="D-100"):
        disruption = self.db.create_disruption("planner-01", {"code": code, "name": "人民广场站台施工", "starts_at": "2026-09-27T20:00:00+08:00", "ends_at": "2026-09-28T01:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "station_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["X1"], "travel_minutes": 6, "effective_start_minute": 1200, "effective_end_minute": 1500}, "planner")
        return disruption, version

    def test_station_relocation_window_walk_and_restore(self):
        _, version = self._relocation_version()
        # Baseline queries never apply the relocation.
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], None, 1430)["minutes"], 5)
        # Inside the window the original stop is sealed: walk to the
        # temporary stop, then ride on. The walk is counted in the total.
        moved = self.db.route(self.stops["S2"], self.stops["S3"], version, 1430)
        self.assertEqual(moved["status"], "ok")
        self.assertEqual(moved["minutes"], 11)
        self.assertEqual(moved["arrival"]["clock"], "00:01")
        walk = [leg for leg in moved["legs"] if leg["kind"] == "walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual((walk[0]["from_stop_id"], walk[0]["to_stop_id"], walk[0]["minutes"]), (self.stops["S2"], self.stops["X1"], 6))
        # The line serves the temporary stop in place of the original one.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S3"], version, 1430)["minutes"], 13)
        # Destinations at the sealed stop walk back from the temporary stop.
        self.assertEqual(self.db.route(self.stops["S3"], self.stops["S2"], version, 1430)["minutes"], 11)
        # Outside the window the original stop is restored.
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], version, 1199)["minutes"], 5)
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], version, 1501)["minutes"], 5)

    def test_station_relocation_accessible_fallback(self):
        _, version = self._relocation_version("D-101")
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["X1"], "accessible": False}, "planner")
        # The temporary stop lacks barrier-free facilities, but the accessible
        # query must still return the walkable route instead of "unreachable".
        route = self.db.route(self.stops["S2"], self.stops["S3"], version, 1430, require_accessible=True)
        self.assertEqual(route["status"], "ok")
        self.assertEqual(route["minutes"], 11)
        walk = [leg for leg in route["legs"] if leg["kind"] == "walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual(walk[0]["warning"], "临时站点不具备无障碍条件")
        # Regular membership of the temporary stop on other lines stays
        # filtered: accessible passengers ride through it without boarding
        # or alighting there (L2 skips X1, S1->S4 accumulates 8+9 minutes).
        through = self.db.route(self.stops["S1"], self.stops["S5"], version, 1430, require_accessible=True)
        self.assertEqual(through["minutes"], 23)
        self.assertEqual(through["path"], [self.stops["S1"], self.stops["S4"], self.stops["S5"]])

    def test_station_relocation_publish_isolation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-102", "name": "人民广场站台施工", "starts_at": "2026-09-27T20:00:00+08:00", "ends_at": "2026-09-28T01:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "station_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["X1"], "travel_minutes": 6, "effective_start_minute": 1200, "effective_end_minute": 1500}, "planner")
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], v2, 1430)["minutes"], 11)
        # The already published version keeps the original stopping pattern.
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], v1, 1430)["minutes"], 5)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], v1, 1430)["minutes"], 5)
        self.assertEqual(self.db.route(self.stops["S2"], self.stops["S3"], v2, 1430)["minutes"], 11)
        snapshot = self.db.get_version(v1)["snapshot"]
        self.assertEqual([c["kind"] for c in snapshot["changes"]], [])

    def test_station_relocation_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-103", "name": "人民广场站台施工", "starts_at": "2026-09-27T20:00:00+08:00", "ends_at": "2026-09-28T01:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        base = {"kind": "station_relocation", "stop_id": self.stops["S2"], "to_stop_id": self.stops["X1"], "travel_minutes": 6, "effective_start_minute": 1200, "effective_end_minute": 1500}
        for missing in ("to_stop_id", "travel_minutes"):
            with self.assertRaises(DomainError):
                self.db.add_change(version, "planner-01", {k: v for k, v in base.items() if k != missing}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_change(version, "planner-01", {k: v for k, v in base.items() if not k.startswith("effective_")}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_change(version, "planner-01", {**base, "to_stop_id": self.stops["S2"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_change(version, "planner-01", {**base, "travel_minutes": -1}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_change(version, "planner-01", {**base, "effective_end_minute": 1200}, "planner")
        self.db.add_change(version, "planner-01", base, "planner")
        # Overlapping windows for the same original stop are rejected.
        with self.assertRaises(DomainError):
            self.db.add_change(version, "planner-01", {**base, "effective_start_minute": 1400, "effective_end_minute": 1600}, "planner")
        # Adjacent windows are allowed.
        self.db.add_change(version, "planner-01", {**base, "effective_start_minute": 1500, "effective_end_minute": 1800}, "planner")


if __name__ == "__main__":
    unittest.main()
