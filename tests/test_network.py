import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class LanAddressTests(unittest.TestCase):
    def test_prefers_active_wifi_over_route_and_fallback(self):
        self.assertEqual(
            server.choose_lan_ip("10.20.4.8", "192.168.1.9", ["172.16.0.3"]),
            "10.20.4.8",
        )

    def test_skips_non_connectable_addresses(self):
        self.assertEqual(
            server.choose_lan_ip("169.254.1.2", "127.0.0.1", ["10.0.0.7"]),
            "10.0.0.7",
        )
        self.assertEqual(server.choose_lan_ip("127.0.0.1", None), "127.0.0.1")

    def test_state_rebuilds_sensor_links_for_current_address(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.Store(root=directory)
            store.state["sensors"] = [{"id": "sensor-1", "token": "pairing-token"}]

            first = store.snapshot("https://10.1.1.2:8443", "ready")
            second = store.snapshot("https://172.16.4.3:8443", "ready")

            self.assertEqual(first["sensors"][0]["pairingUrl"],
                             "https://10.1.1.2:8443/sensor?sid=sensor-1&token=pairing-token")
            self.assertEqual(second["sensors"][0]["pairingUrl"],
                             "https://172.16.4.3:8443/sensor?sid=sensor-1&token=pairing-token")

    def test_running_server_switches_url_and_reloads_certificate(self):
        class FakeTlsContext:
            def __init__(self):
                self.loaded = None

            def load_cert_chain(self, certfile, keyfile):
                self.loaded = (certfile, keyfile)

        context = FakeTlsContext()
        http_server = object.__new__(server.SoundServer)
        http_server.lan_ip = "10.0.0.4"
        http_server.port = 8443
        http_server.base_url = "https://10.0.0.4:8443"
        http_server.cert_path = Path("server.crt")
        http_server.key_path = Path("server.key")
        http_server.tls_context = context
        http_server.network_lock = threading.RLock()
        with patch("server.ensure_certificate", return_value=(Path("ca"), Path("new.crt"), Path("new.key"))) as issue:
            self.assertTrue(http_server.update_network_address("192.168.1.12"))
            self.assertEqual(http_server.current_base_url(), f"https://192.168.1.12:{http_server.port}")
            self.assertEqual(context.loaded, (Path("server.crt"), Path("server.key")))
            issue.assert_called_once_with(["192.168.1.12"])
            self.assertFalse(http_server.update_network_address("192.168.1.12"))

    def test_sensor_audio_metadata_is_saved_for_the_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.Store(root=directory)
            sensor = {"id": "esp32-1", "name": "INMP441", "x": 0.3, "y": 0.3}
            event_id, _, _ = store.add_observation(
                sensor,
                {"capturedAt": server.now_iso(), "peakDbfs": -18.5, "duration": 1.2,
                 "sampleRate": 16000, "firmware": "esp32-inmp441-v1"},
                b"fake wav for the store layer",
            )
            observation = store.state["events"][-1]["observations"][0]
            self.assertEqual(store.state["events"][-1]["id"], event_id)
            self.assertEqual(observation["sampleRate"], 16000)
            self.assertEqual(observation["firmware"], "esp32-inmp441-v1")


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required by SoundDetect")
class CertificateAddressTests(unittest.TestCase):
    def test_rotates_leaf_san_without_replacing_trusted_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            cert_dir = Path(directory)
            ca, leaf, _ = server.ensure_certificate(["192.168.1.9"], cert_dir=cert_dir)
            trusted_ca = ca.read_bytes()
            first_leaf = leaf.read_bytes()

            server.ensure_certificate(["192.168.1.9"], cert_dir=cert_dir)
            self.assertEqual(leaf.read_bytes(), first_leaf, "unchanged address should not reissue the certificate")

            server.ensure_certificate(["10.2.3.4"], cert_dir=cert_dir)
            self.assertEqual(ca.read_bytes(), trusted_ca, "Wi-Fi changes must preserve the installed CA")
            details = subprocess.check_output(
                ["openssl", "x509", "-in", str(leaf), "-noout", "-text"],
                text=True,
            )
            normalized = details.replace(" ", "")
            self.assertIn("IPAddress:10.2.3.4", normalized)
            self.assertNotIn("IPAddress:192.168.1.9", normalized)


if __name__ == "__main__":
    unittest.main()
