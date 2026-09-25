import unittest
from unittest.mock import call, patch

import app


class ApiTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()
        app.calibration_state["zero_complete"] = False
        app.calibration_state["points"] = {}

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

    @patch("app.read_params", return_value={"dp": 2, "min_div": 50, "max_range_raw": 300000})
    def test_params_rejects_invalid_final_min_div_and_range_combination(self, _read_params):
        response = self.client.post("/api/params", json={"min_div": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("100000", response.json["error"])

    @patch("app.device.read_registers", return_value=[3])
    @patch("app.device.write_u32")
    @patch("app.device.write_register")
    @patch("app.read_params")
    def test_params_modifies_only_dp(self, read_params, write_register, write_u32, _read_registers):
        current = {"dp": 2, "min_div": 5, "max_range_raw": 300000}
        read_params.side_effect = [current, {**current, "dp": 3}]
        response = self.client.post("/api/params", json={"dp": 3})
        self.assertEqual(response.status_code, 200)
        write_register.assert_called_once_with(0x0012, 3)
        write_u32.assert_not_called()

    @patch("app.device.read_registers", return_value=[0x0001, 0x86A0])
    @patch("app.device.write_u32")
    @patch("app.device.write_register")
    @patch("app.read_params")
    def test_params_modifies_only_max_range(self, read_params, write_register, write_u32, _read_registers):
        current = {"dp": 2, "min_div": 1, "max_range_raw": 50000}
        read_params.side_effect = [current, {**current, "max_range_raw": 100000}]
        response = self.client.post("/api/params", json={"max_range_g": 1000})
        self.assertEqual(response.status_code, 200)
        write_register.assert_not_called()
        write_u32.assert_called_once_with(0x0014, 100000)

    @patch("app.read_params")
    def test_params_combined_dp_and_max_range_uses_target_dp_and_order(self, read_params):
        current = {"dp": 2, "min_div": 50, "max_range_raw": 50000}
        read_params.side_effect = [current, {**current, "dp": 3, "max_range_raw": 1000000}]
        operations = []

        def write_register(register, value):
            operations.append(("write_register", register, value))

        def write_u32(register, value):
            operations.append(("write_u32", register, value))

        def read_registers(register, count):
            return [3] if (register, count) == (0x0012, 1) else [0x000F, 0x4240]

        with patch("app.device.write_register", side_effect=write_register), \
             patch("app.device.write_u32", side_effect=write_u32), \
             patch("app.device.read_registers", side_effect=read_registers):
            # Deliberately send max_range_g before dp; server ordering must not depend on JSON order.
            response = self.client.post("/api/params", json={"max_range_g": 1000, "dp": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(operations, [
            ("write_register", 0x0012, 3),
            ("write_u32", 0x0014, 1000000),
        ])

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
        app.calibration_state["zero_complete"] = True
        response = self.client.post(
            "/api/calibrate", json={"weight": 100, "point": 1, "confirm": True}
        )
        self.assertEqual(response.status_code, 200)
        write.assert_called_once_with(0x001E, 10000)

    @patch("app.device.write_u32")
    @patch("app.read_status", return_value={"stable": True, "overload": False, "dp": 2})
    def test_all_loading_point_register_addresses(self, _status, write):
        app.calibration_state["zero_complete"] = True
        weights = (500, 1000, 2000, 2800)
        for point, weight in enumerate(weights, start=1):
            with self.subTest(point=point):
                response = self.client.post(
                    "/api/calibrate",
                    json={"weight": weight, "point": point, "confirm": True},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(write.call_args_list, [
            call(0x001E, 50000), call(0x0020, 100000),
            call(0x0022, 200000), call(0x0024, 280000),
        ])

    @patch("app.read_status", return_value={"stable": True, "overload": False, "dp": 2})
    def test_calibration_requires_zero_and_point_order(self, _status):
        response = self.client.post("/api/calibrate", json={"weight": 500, "point": 1, "confirm": True})
        self.assertEqual(response.status_code, 409)
        app.calibration_state["zero_complete"] = True
        response = self.client.post("/api/calibrate", json={"weight": 1000, "point": 2, "confirm": True})
        self.assertEqual(response.status_code, 409)

    @patch("app.device.write_u32")
    @patch("app.read_status", return_value={"stable": True, "overload": False, "dp": 2})
    def test_rerecording_point_invalidates_only_downstream_points(self, _status, write):
        app.calibration_state["zero_complete"] = True
        app.calibration_state["points"] = {
            "1": {"weight": 500}, "2": {"weight": 1000},
            "3": {"weight": 2000}, "4": {"weight": 2800},
        }
        no_confirm = self.client.post(
            "/api/calibrate", json={"weight": 1100, "point": 2, "confirm": True}
        )
        self.assertEqual(no_confirm.status_code, 409)
        response = self.client.post(
            "/api/calibrate", json={"weight": 1100, "point": 2, "confirm": True, "overwrite": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["invalidated"], [3, 4])
        self.assertEqual(set(response.json["state"]["points"]), {"1", "2"})
        write.assert_called_once_with(0x0020, 110000)


class ProtocolTests(unittest.TestCase):
    def test_crc_matches_documented_weight_request(self):
        frame = bytes.fromhex("010300000003")
        self.assertEqual(app.crc16(frame), 0xCB05)

    def test_signed_u32(self):
        self.assertEqual(app.signed_u32(0xFFFF, 0xFFFF), -1)
        self.assertEqual(app.signed_u32(0x0001, 0x0002), 65538)


if __name__ == "__main__":
    unittest.main()
