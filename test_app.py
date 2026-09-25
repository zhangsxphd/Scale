import unittest
from unittest.mock import patch

import app


class ApiTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def test_health_does_not_touch_hardware(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_config_rejects_unsupported_baudrate(self):
        response = self.client.post("/api/config", json={"baudrate": 12345})
        self.assertEqual(response.status_code, 400)

    def test_params_reject_unknown_and_out_of_range_values(self):
        self.assertEqual(self.client.post("/api/params", json={"bad": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/params", json={"dp": 8}).status_code, 400)
        self.assertEqual(self.client.post("/api/params", json={"max_range_g": 3001}).status_code, 400)

    def test_calibration_requires_confirmation(self):
        response = self.client.post("/api/calibrate", json={"weight": 100, "point": 1})
        self.assertEqual(response.status_code, 400)

    @patch("app.read_status", return_value={"stable": True, "overload": False, "dp": 2})
    @patch("app.device.write_u32")
    def test_zero_calibration_uses_vendor_zero_register(self, write, _status):
        response = self.client.post("/api/cal_zero", json={"confirm": True})
        self.assertEqual(response.status_code, 200)
        write.assert_called_once_with(0x0016, 1)

    @patch("app.read_status", return_value={"stable": False, "overload": False, "dp": 2})
    def test_calibration_rejects_unstable_reading(self, _status):
        response = self.client.post(
            "/api/calibrate", json={"weight": 100, "point": 1, "confirm": True}
        )
        self.assertEqual(response.status_code, 409)

    @patch("app.read_params", return_value={"max_range_g": 300})
    @patch("app.read_status", return_value={"stable": True, "overload": True, "dp": 2})
    def test_calibration_reports_overload_reason(self, _status, _params):
        response = self.client.post(
            "/api/calibrate", json={"weight": 500, "point": 1, "confirm": True}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("300 g", response.json["error"])

    @patch("app.device.write_u32")
    @patch("app.read_status", return_value={"stable": True, "overload": False, "dp": 2})
    def test_calibration_scales_weight_and_writes_selected_point(self, _status, write):
        response = self.client.post(
            "/api/calibrate", json={"weight": 100, "point": 1, "confirm": True}
        )
        self.assertEqual(response.status_code, 200)
        write.assert_called_once_with(0x001E, 10000)


class ProtocolTests(unittest.TestCase):
    def test_crc_matches_documented_weight_request(self):
        frame = bytes.fromhex("010300000003")
        self.assertEqual(app.crc16(frame), 0xCB05)

    def test_signed_u32(self):
        self.assertEqual(app.signed_u32(0xFFFF, 0xFFFF), -1)
        self.assertEqual(app.signed_u32(0x0001, 0x0002), 65538)


if __name__ == "__main__":
    unittest.main()
