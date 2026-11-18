import tempfile
import time
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import server


def iso_at(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def add_classified_sound(store, sensor, label, timestamp):
    event_id, observation_id, _ = store.add_observation(
        sensor,
        {"capturedAt": iso_at(timestamp), "peakDbfs": -20, "duration": 1.0},
        b"test audio",
    )
    store.finish_observation(
        event_id,
        observation_id,
        {"label": label, "confidence": 0.82, "candidates": []},
    )
    return event_id


class SensorNodeTests(unittest.TestCase):
    def test_creates_mobile_and_esp32_node_types(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.Store(root=directory)

            mobile = store.add_sensor("mobile")
            esp32 = store.add_sensor("esp32_inmp441")
            snapshot = store.snapshot("https://10.0.0.5:8443", "ready")

            self.assertEqual(mobile["nodeType"], "mobile")
            self.assertEqual(esp32["nodeType"], "esp32_inmp441")
            self.assertIn("ESP32 + INMP441", esp32["name"])
            self.assertEqual(snapshot["sensors"][1]["nodeType"], "esp32_inmp441")
            with self.assertRaises(ValueError):
                store.add_sensor("unknown")

    def test_node_and_event_rule_api_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.Store(root=directory)

            def handler(path, payload=None):
                instance = server.Handler.__new__(server.Handler)
                instance.path = path
                instance.server = SimpleNamespace(store=store)
                instance.admin = lambda: True
                instance.json_body = lambda: payload
                instance.respond = lambda status, data=None, *args, **kwargs: (status, data)
                return instance

            status, sensor = handler("/api/sensors", {"nodeType": "esp32_inmp441"}).do_POST()
            self.assertEqual(status, 201)
            self.assertEqual(sensor["nodeType"], "esp32_inmp441")

            status, rule = handler("/api/event-rules", {
                "name": "Door entry", "firstMatch": "metal tool", "secondMatch": "door open",
                "windowSeconds": 35, "scope": "any_sensor",
            }).do_POST()
            self.assertEqual(status, 201)
            status, changed = handler("/api/event-rules/" + rule["id"], {"enabled": False}).do_PATCH()
            self.assertEqual(status, 200)
            self.assertFalse(changed["enabled"])
            status, deleted = handler("/api/event-rules/" + rule["id"]).do_DELETE()
            self.assertEqual(status, 200)
            self.assertEqual(deleted["deleted"], rule["id"])


class IncidentRuleTests(unittest.TestCase):
    def make_store(self, directory, scope="any_sensor", window=45):
        store = server.Store(root=directory)
        rule = store.create_event_rule({
            "name": "Breaking and Entering",
            "firstMatch": "lock picking",
            "secondMatch": "opening door",
            "windowSeconds": window,
            "scope": scope,
        })
        return store, rule

    def test_ordered_sound_sequence_creates_one_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            store, rule = self.make_store(directory)
            sensor = store.add_sensor("esp32_inmp441")
            first_time = time.time() + 1

            first_id = add_classified_sound(store, sensor, "Lock Picking", first_time)
            second_id = add_classified_sound(store, sensor, "Opening Door", first_time + 20)

            incidents = store.state["incidents"]
            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0]["ruleId"], rule["id"])
            self.assertEqual(incidents[0]["sourceEventIds"], [first_id, second_id])
            self.assertEqual(incidents[0]["ruleName"], "Breaking and Entering")
            self.assertEqual(len(incidents[0]["sequence"]), 2)

            store._evaluate_event_rules()
            self.assertEqual(len(store.state["incidents"]), 1)

    def test_sequence_still_matches_when_classification_finishes_out_of_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _ = self.make_store(directory)
            sensor = store.add_sensor("mobile")
            first_time = time.time() + 1
            first_event, first_observation, _ = store.add_observation(
                sensor, {"capturedAt": iso_at(first_time)}, b"first audio",
            )
            second_event, second_observation, _ = store.add_observation(
                sensor, {"capturedAt": iso_at(first_time + 20)}, b"second audio",
            )

            store.finish_observation(
                second_event, second_observation,
                {"label": "Opening Door", "confidence": 0.8, "candidates": []},
            )
            self.assertEqual(store.state["incidents"], [])
            store.finish_observation(
                first_event, first_observation,
                {"label": "Lock Picking", "confidence": 0.8, "candidates": []},
            )
            self.assertEqual(len(store.state["incidents"]), 1)

    def test_ignores_wrong_order_out_of_window_and_different_sensor_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _ = self.make_store(directory, scope="same_sensor", window=30)
            first_sensor = store.add_sensor("mobile")
            second_sensor = store.add_sensor("esp32_inmp441")
            first_time = time.time() + 1

            add_classified_sound(store, first_sensor, "Opening Door", first_time)
            add_classified_sound(store, first_sensor, "Lock Picking", first_time + 5)
            add_classified_sound(store, first_sensor, "Lock Picking", first_time + 10)
            add_classified_sound(store, second_sensor, "Opening Door", first_time + 20)
            add_classified_sound(store, first_sensor, "Opening Door", first_time + 50)

            self.assertEqual(store.state["incidents"], [])

    def test_same_sensor_sequence_and_rule_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            store, rule = self.make_store(directory, scope="same_sensor", window=30)
            sensor = store.add_sensor("mobile")
            first_time = time.time() + 1

            add_classified_sound(store, sensor, "Lock Picking", first_time)
            add_classified_sound(store, sensor, "Opening Door", first_time + 10)
            self.assertEqual(len(store.state["incidents"]), 1)

            store.update_event_rule(rule["id"], {"enabled": False})
            self.assertFalse(next(item for item in store.state["eventRules"] if item["id"] == rule["id"])["enabled"])
            next_sequence_time = time.time() + 30
            add_classified_sound(store, sensor, "Lock Picking", next_sequence_time)
            add_classified_sound(store, sensor, "Opening Door", next_sequence_time + 10)
            self.assertEqual(len(store.state["incidents"]), 1)


if __name__ == "__main__":
    unittest.main()
