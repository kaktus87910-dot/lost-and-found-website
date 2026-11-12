import base64
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as web_server
from server import LocalServer


JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP////////////////////////////////////////////////////////////////////////////////"
    "////////////////////////////2wBDAf////////////////////////////////////////////////////////////////////////////////"
    "////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/"
    "xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgB"
    "AQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aA"
    "AgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP"
    "/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFB"
    "EBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9"
    "k="
)


class LostFoundWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global JPEG
        image = web_server.np.full((8, 8, 3), 255, dtype=web_server.np.uint8)
        ok, encoded = web_server.cv2.imencode(".jpg", image)
        if not ok:
            raise RuntimeError("Could not build the JPEG test fixture.")
        JPEG = base64.b64encode(encoded).decode("ascii")
        cls.password = "render-test-" + secrets.token_urlsafe(24)
        cls.password_hash = web_server.make_password_hash(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lf-web-test-")
        self.server = LocalServer(
            ("127.0.0.1", 0),
            Path(self.temp.name),
            admin_password_hash=self.password_hash,
            session_secret=secrets.token_bytes(32),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = ""
        self.csrf_token = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, payload=None, method=None, extra_headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        actual_method = method or ("POST" if data is not None else "GET")
        headers = {"Origin": self.base}
        if data:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf_token and actual_method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = self.csrf_token
        if extra_headers:
            headers.update(extra_headers)
        request = Request(self.base + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=3) as response:
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";", 1)[0]
            body = response.read()
            if path == "/api/auth/login" and response.status == 200:
                self.csrf_token = json.loads(body)["csrf_token"]
            if path == "/api/auth/logout" and response.status == 200:
                self.cookie = ""
                self.csrf_token = ""
            return response.status, response.headers, body

    def request_error(self, path, payload=None, method=None, extra_headers=None):
        with self.assertRaises(HTTPError) as error:
            self.request(path, payload, method, extra_headers)
        return error.exception.code, error.exception.read().decode("utf-8")

    def create(self, kind, rgb, description="same label"):
        photo = base64.b64decode(JPEG)
        color = list(rgb)
        features = [0.2, 0.3, 0.4, 0.5, 0.6]
        detection = {"object_class": 39, "color": color, "features": features}
        return self.request("/api/reports", {
            "kind": kind,
            "object_class": 39,
            "description": description,
            "photo": "data:image/jpeg;base64," + JPEG,
            "color": color,
            "features": features,
            "detection_token": web_server.detection_token(
                self.server.session_secret, photo, detection,
            ),
        })[2]

    def login(self):
        return self.request("/api/auth/login", {"password": self.password}, "POST")

    def test_server_refuses_to_start_without_admin_secrets(self):
        with self.assertRaisesRegex(ValueError, "ADMIN_PASSWORD_HASH"):
            LocalServer(
                ("127.0.0.1", 0), Path(self.temp.name),
                admin_password_hash=None, session_secret=None,
            )

    @unittest.skipIf(
        web_server.cv2 is None or not web_server.DEFAULT_MODEL.is_file(),
        "Python OpenCV bindings or the local ONNX model are unavailable",
    )
    def test_local_yolo_api_uses_configured_model(self):
        image = web_server.np.full((96, 128, 3), 255, dtype=web_server.np.uint8)
        ok, encoded = web_server.cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        payload = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")

        health = json.loads(self.request("/api/health")[2])
        self.assertTrue(health["detection"]["available"], health["detection"]["error"])
        status, _, response = self.request("/api/detect", {"photo": payload})
        self.assertEqual(status, 200)
        result = json.loads(response)
        self.assertIsInstance(result["detections"], list)
        self.assertEqual(result["detections"], [])
        for detection in result["detections"]:
            self.assertGreater(detection["object_class"], 0)
            self.assertIn(detection["object_name"], web_server.COCO_CLASSES)
            self.assertGreaterEqual(detection["confidence"], 0.25)
            self.assertEqual(len(detection["color"]), 3)
            self.assertEqual(len(detection["features"]), 5)

        corrupt_photo = "data:image/jpeg;base64," + base64.b64encode(
            b"\xff\xd8broken\xff\xd9"
        ).decode("ascii")
        with self.assertRaises(HTTPError) as image_error:
            self.request("/api/detect", {"photo": corrupt_photo})
        self.assertEqual(image_error.exception.code, 400)

    @unittest.skipIf(
        web_server.cv2 is None,
        "Python OpenCV bindings are unavailable",
    )
    def test_detected_class_and_roi_color_can_create_report(self):
        image = web_server.np.full((64, 64, 3), (0, 0, 255), dtype=web_server.np.uint8)
        color, color_name, features = web_server.analyze_region(image)
        self.assertEqual(color, [255, 0, 0])
        self.assertEqual(color_name, "red")
        self.assertEqual(len(features), 5)
        self.assertTrue(all(0 <= value <= 1 for value in features))

        class FakeDetector:
            def detect(self, _photo):
                return [{
                    "object_class": 67,
                    "object_name": "cell phone",
                    "confidence": 0.91,
                    "bbox": [4, 5, 40, 50],
                    "color": color,
                    "color_name": color_name,
                    "features": features,
                }]

        self.server._detector = FakeDetector()
        ok, encoded = web_server.cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        photo = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        detections = json.loads(self.request("/api/detect", {"photo": photo})[2])["detections"]
        detected = detections[0]
        reports = []
        for kind in ("lost", "found"):
            result = json.loads(self.request("/api/reports", {
                "kind": kind,
                "object_class": detected["object_class"],
                "description": "red phone",
                "photo": photo,
                "color": detected["color"],
                "features": detected["features"],
                "detection_token": detected["detection_token"],
            })[2])
            reports.append(result["report"])
        self.assertEqual(reports[0]["object_class"], 67)
        self.assertEqual(reports[0]["color"], [255, 0, 0])
        self.assertEqual(len(result["matches"]), 1)
        listed = json.loads(self.request("/api/reports")[2])
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(report["color"] == [255, 0, 0] for report in listed))
        self.assertTrue(all(report["color_name"] == "red" for report in listed))

    @unittest.skipIf(
        web_server.cv2 is None,
        "Python OpenCV bindings are unavailable",
    )
    def test_color_uses_center_of_detected_object_and_is_named_in_reports(self):
        image = web_server.np.zeros((90, 90, 3), dtype=web_server.np.uint8)
        image[:, :] = (255, 0, 0)
        image[15:75, 15:75] = (0, 0, 255)
        color, color_name, _ = web_server.analyze_region(image)
        self.assertEqual(color, [255, 0, 0])
        self.assertEqual(color_name, "red")

        with tempfile.TemporaryDirectory(prefix="lf-color-test-") as temp:
            db = web_server.open_store(Path(temp))
            try:
                cursor = db.execute(
                    """INSERT INTO reports(
                       kind, object_class, object_name, description, photo_path,
                       color_b, color_g, color_r, texture, edge_density,
                       saturation, brightness, entropy)
                       VALUES ('lost', 39, 'bottle', '', '/test.jpg',
                               0, 0, 255, 0, 0, 0, 0, 0)"""
                )
                db.commit()
                row = db.execute(
                    "SELECT * FROM reports WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                self.assertEqual(web_server.as_report(row)["color_name"], "red")
            finally:
                db.close()

    def test_color_names_are_english(self):
        samples = {
            "black": (0, 0, 0),
            "white": (255, 255, 255),
            "gray": (128, 128, 128),
            "red": (0, 0, 255),
            "orange": (0, 128, 255),
            "yellow": (0, 255, 255),
            "green": (0, 255, 0),
            "teal": (255, 255, 0),
            "blue": (255, 0, 0),
            "purple": (255, 0, 128),
            "magenta": (255, 0, 255),
        }
        for expected, bgr in samples.items():
            with self.subTest(color=expected):
                self.assertEqual(web_server.describe_color(bgr), expected)

    def test_missing_model_fails_detection_without_manual_fallback(self):
        self.server.model_path = Path(self.temp.name) / "missing.onnx"
        health = json.loads(self.request("/api/health")[2])
        self.assertFalse(health["detection"]["available"])
        expected_error = "OpenCV" if web_server.cv2 is None else "not found"
        self.assertIn(expected_error.casefold(), health["detection"]["error"].casefold())

        with self.assertRaises(HTTPError) as detection_error:
            self.request("/api/detect", {"photo": "data:image/jpeg;base64," + JPEG})
        self.assertEqual(detection_error.exception.code, 503)

    def test_detector_uses_overlapping_tiles_for_large_images(self):
        if web_server.cv2 is None:
            self.skipTest("Python OpenCV bindings are unavailable")
        self.assertEqual(web_server.OpenCVDNNDetector.tile_origins(640), [0])
        self.assertEqual(web_server.OpenCVDNNDetector.tile_origins(800), [0])
        self.assertEqual(web_server.OpenCVDNNDetector.tile_origins(1200), [0, 400])
        self.assertEqual(web_server.OpenCVDNNDetector.tile_origins(1600), [0, 640, 800])

        detector = object.__new__(web_server.OpenCVDNNDetector)
        calls = []

        def fake_inference(region, offset_x=0, offset_y=0):
            calls.append((region.shape[1], region.shape[0], offset_x, offset_y))
            return [], [], []

        detector.infer_region = fake_inference
        image = web_server.np.zeros((1200, 1200, 3), dtype=web_server.np.uint8)
        ok, encoded = web_server.cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        self.assertEqual(detector.detect(encoded.tobytes()), [])
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0], (1200, 1200, 0, 0))
        self.assertEqual({(call[2], call[3]) for call in calls[1:]},
                         {(0, 0), (400, 0), (0, 400), (400, 400)})

    def test_overlapping_different_classes_are_not_suppressed(self):
        if web_server.cv2 is None:
            self.skipTest("Python OpenCV bindings are unavailable")
        detector = object.__new__(web_server.OpenCVDNNDetector)

        def fake_inference(_region, offset_x=0, offset_y=0):
            if offset_x or offset_y:
                return [], [], []
            return [[8, 8, 32, 32], [8, 8, 32, 32]], [0.9, 0.8], [39, 67]

        detector.infer_region = fake_inference
        image = web_server.np.full((48, 48, 3), (0, 0, 255), dtype=web_server.np.uint8)
        ok, encoded = web_server.cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        detections = detector.detect(encoded.tobytes())
        self.assertEqual({item["object_class"] for item in detections}, {39, 67})

    def test_legacy_person_reports_are_hidden_from_lists_and_matches(self):
        db = web_server.open_store(Path(self.temp.name))
        try:
            for kind in ("lost", "found"):
                db.execute(
                    """INSERT INTO reports(
                       kind, object_class, object_name, description, photo_path,
                       color_b, color_g, color_r, texture, edge_density,
                       saturation, brightness, entropy)
                       VALUES (?, 0, 'person', '', '/legacy-person.jpg',
                               0, 0, 0, 0, 0, 0, 0, 0)""",
                    (kind,),
                )
            db.commit()
            person_ids = [
                row[0] for row in db.execute(
                    "SELECT id FROM reports WHERE object_class = 0 ORDER BY id"
                )
            ]
            self.assertEqual(web_server.find_matches(db, person_ids[0]), [])
        finally:
            db.close()

        reports = json.loads(self.request("/api/reports")[2])
        self.assertEqual(reports, [])
        matches = json.loads(self.request(f"/api/matches/{person_ids[0]}")[2])
        self.assertEqual(matches, [])
        with self.assertRaises(HTTPError) as confirm_error:
            self.login()
            self.request("/api/matches/confirm", {
                "report_id": person_ids[0], "candidate_id": person_ids[1],
            })
        self.assertEqual(confirm_error.exception.code, 400)

    def test_local_site_registration_matching_photo_and_confirmation(self):
        status, headers, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = page.decode("utf-8")
        self.assertIn('<html lang="en"', html)
        self.assertIn("I lost something", html)
        self.assertIn("Detected item", html)
        for path, content_type in (
            ("/app.js", "javascript"),
            ("/styles.css", "text/css"),
        ):
            asset_status, asset_headers, _ = self.request(path)
            self.assertEqual(asset_status, 200)
            self.assertIn(content_type, asset_headers.get("Content-Type", ""))

        health = json.loads(self.request("/api/health")[2])
        self.assertIn("detection", health)
        self.assertIn("available", health["detection"])

        classes = json.loads(self.request("/api/classes")[2])
        self.assertEqual(len(classes), 79)
        self.assertNotIn("person", [item["name"] for item in classes])

        lost = json.loads(self.create("lost", [200, 30, 20]))
        found = json.loads(self.create("found", [198, 32, 20]))
        self.assertEqual(lost["report"]["kind"], "lost")
        self.assertEqual(len(lost["matches"]), 0)
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["matches"][0]["report"]["id"], lost["report"]["id"])
        self.assertGreater(found["matches"][0]["score"], 0.9)

        photo_status, photo_headers, photo = self.request(lost["report"]["photo_url"])
        self.assertEqual(photo_status, 200)
        self.assertEqual(photo_headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(photo, base64.b64decode(JPEG))

        self.login()
        result = json.loads(self.request("/api/matches/confirm", {
            "report_id": lost["report"]["id"],
            "candidate_id": found["report"]["id"],
        })[2])
        self.assertEqual(result["closed_ids"], [lost["report"]["id"], found["report"]["id"]])
        reports = json.loads(self.request("/api/reports")[2])
        self.assertTrue(all(report["status"] == "matched" for report in reports))
        self.assertEqual(json.loads(self.request(
            f"/api/matches/{lost['report']['id']}")[2]), [])
        health = json.loads(self.request("/api/health")[2])
        self.assertEqual(health["open_reports"], 0)

    def test_invalid_person_report_and_same_kind_confirm_are_rejected(self):
        with self.assertRaises(HTTPError) as person_error:
            self.request("/api/reports", {
                "kind": "lost", "object_class": 0, "description": "",
                "photo": "data:image/jpeg;base64," + JPEG,
                "color": [0, 0, 0], "features": [0, 0, 0, 0, 0],
            })
        self.assertEqual(person_error.exception.code, 400)
        self.assertIn("other than person", person_error.exception.read().decode())

        lost = json.loads(self.create("lost", [10, 20, 30]))["report"]
        another_lost = json.loads(self.create("lost", [20, 30, 40]))["report"]
        self.login()
        with self.assertRaises(HTTPError) as pair_error:
            self.request("/api/matches/confirm", {
                "report_id": lost["id"], "candidate_id": another_lost["id"],
            })
        self.assertEqual(pair_error.exception.code, 400)

    def test_open_report_edit_updates_details_and_recalculates_matches(self):
        lost = json.loads(self.create("lost", [10, 20, 30]))["report"]
        found = json.loads(self.create("found", [12, 21, 31]))["report"]
        original_class = found["object_class"]
        original_color = found["color"]
        original_photo = found["photo_url"]

        self.login()
        status, _, body = self.request(
            f"/api/reports/{found['id']}",
            {"kind": "lost", "description": "blue item with a sticker"},
            method="PUT",
        )
        self.assertEqual(status, 200)
        updated = json.loads(body)
        self.assertEqual(updated["report"]["kind"], "lost")
        self.assertEqual(updated["report"]["description"], "blue item with a sticker")
        self.assertEqual(updated["report"]["object_class"], original_class)
        self.assertEqual(updated["report"]["color"], original_color)
        self.assertEqual(updated["report"]["photo_url"], original_photo)
        self.assertEqual(updated["matches"], [])
        self.assertEqual(json.loads(self.request("/api/matches/" + str(lost["id"]))[2]), [])
        photo_status, _, _ = self.request(original_photo)
        self.assertEqual(photo_status, 200)

    def test_edit_validation_and_missing_ids(self):
        report = json.loads(self.create("lost", [10, 20, 30]))["report"]
        self.login()
        self.assertEqual(self.request_error(
            f"/api/reports/{report['id']}", {"kind": "other", "description": ""},
            method="PUT",
        )[0], 400)
        self.assertEqual(self.request_error(
            f"/api/reports/{report['id']}", {"kind": "lost", "description": "x" * 1001},
            method="PUT",
        )[0], 400)
        self.assertEqual(self.request_error(
            f"/api/reports/{report['id']}", {"kind": "lost", "description": "", "color": [0, 0, 0]},
            method="PUT",
        )[0], 400)
        self.assertEqual(self.request_error(
            "/api/reports/not-an-id", {"kind": "lost", "description": ""}, method="PUT",
        )[0], 400)
        self.assertEqual(self.request_error(
            "/api/reports/99999", {"kind": "lost", "description": ""}, method="PUT",
        )[0], 404)
        self.assertEqual(self.request_error("/api/reports/not-an-id", method="DELETE")[0], 400)
        self.assertEqual(self.request_error("/api/reports/99999", method="DELETE")[0], 404)

    def test_put_report_route_is_handled_by_application_not_http_server(self):
        self.login()
        status, body = self.request_error(
            "/api/reports/99999",
            {"kind": "lost", "description": "route probe"},
            method="PUT",
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "Report not found."})

    def test_delete_removes_unreferenced_photo_and_preserves_shared_photo(self):
        first = json.loads(self.create("lost", [10, 20, 30]))["report"]
        second = json.loads(self.create("found", [20, 30, 40]))["report"]
        db = web_server.open_store(Path(self.temp.name))
        try:
            first_path = db.execute(
                "SELECT photo_path FROM reports WHERE id=?", (first["id"],)
            ).fetchone()["photo_path"]
            db.execute(
                "UPDATE reports SET photo_path=? WHERE id=?", (first_path, second["id"])
            )
            db.commit()
        finally:
            db.close()

        self.login()
        status, _, body = self.request(f"/api/reports/{first['id']}", method="DELETE")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["deleted"])
        self.assertFalse(result["photo_removed"])
        self.assertTrue(result["photo_in_use"])
        self.assertTrue(Path(first_path).is_file())
        self.assertEqual(len(json.loads(self.request("/api/reports")[2])), 1)

        status, _, body = self.request(f"/api/reports/{second['id']}", method="DELETE")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["photo_removed"])
        self.assertFalse(result["photo_in_use"])
        self.assertFalse(Path(first_path).exists())
        self.assertEqual(json.loads(self.request("/api/reports")[2]), [])

    def test_delete_removes_its_unshared_photo(self):
        report = json.loads(self.create("lost", [10, 20, 30]))["report"]
        db = web_server.open_store(Path(self.temp.name))
        try:
            photo_path = db.execute(
                "SELECT photo_path FROM reports WHERE id=?", (report["id"],)
            ).fetchone()["photo_path"]
        finally:
            db.close()
        self.assertTrue(Path(photo_path).is_file())
        self.login()
        status, _, body = self.request(f"/api/reports/{report['id']}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["photo_removed"])
        self.assertFalse(Path(photo_path).exists())

    def test_matched_reports_cannot_be_edited_or_deleted(self):
        lost = json.loads(self.create("lost", [10, 20, 30]))["report"]
        found = json.loads(self.create("found", [12, 21, 31]))["report"]
        self.login()
        self.request("/api/matches/confirm", {
            "report_id": lost["id"], "candidate_id": found["id"],
        })

        for report in (lost, found):
            status, body = self.request_error(
                f"/api/reports/{report['id']}",
                {"kind": report["kind"], "description": "changed"},
                method="PUT",
            )
            self.assertEqual(status, 409)
            self.assertIn("cannot be edited", body)
            status, body = self.request_error(
                f"/api/reports/{report['id']}", method="DELETE",
            )
            self.assertEqual(status, 409)
            self.assertIn("cannot be deleted", body)
            self.assertEqual(self.request(report["photo_url"])[0], 200)

        reports = json.loads(self.request("/api/reports")[2])
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report["status"] == "matched" for report in reports))

    def test_public_views_and_creation_do_not_grant_admin_mutations(self):
        auth = json.loads(self.request("/api/auth")[2])
        self.assertFalse(auth["is_admin"])
        lost = json.loads(self.create("lost", [10, 20, 30]))["report"]
        found = json.loads(self.create("found", [12, 21, 31]))["report"]
        self.assertEqual(len(json.loads(self.request("/api/reports")[2])), 2)
        self.assertEqual(self.request_error(
            f"/api/reports/{lost['id']}",
            {"kind": "lost", "description": "unauthorized"},
            method="PUT",
        )[0], 401)
        self.assertEqual(self.request_error(
            f"/api/reports/{lost['id']}", method="DELETE",
        )[0], 401)
        self.assertEqual(self.request_error(
            "/api/matches/confirm",
            {"report_id": lost["id"], "candidate_id": found["id"]},
        )[0], 401)

    def test_login_csrf_logout_and_admin_mutations(self):
        report = json.loads(self.create("lost", [10, 20, 30]))["report"]
        self.server.secure_cookies = True
        _, login_headers, _ = self.login()
        auth = json.loads(self.request("/api/auth")[2])
        self.assertTrue(auth["is_admin"])
        self.assertTrue(auth["csrf_token"])
        self.assertIn("HttpOnly", login_headers.get("Set-Cookie", ""))
        self.assertIn("Secure", login_headers.get("Set-Cookie", ""))

        rejected = self.request_error(
            f"/api/reports/{report['id']}",
            {"kind": "found", "description": "blocked without CSRF"},
            method="PUT",
            extra_headers={"X-CSRF-Token": "invalid"},
        )
        self.assertEqual(rejected[0], 403)
        self.assertEqual(self.request(
            f"/api/reports/{report['id']}",
            {"kind": "found", "description": "allowed with CSRF"},
            method="PUT",
        )[0], 200)

        self.request("/api/auth/logout", {}, "POST")
        self.assertFalse(json.loads(self.request("/api/auth")[2])["is_admin"])
        self.assertEqual(self.request_error(
            f"/api/reports/{report['id']}",
            {"kind": "lost", "description": ""},
            method="PUT",
        )[0], 401)

    def test_cross_origin_writes_are_rejected(self):
        status, body = self.request_error(
            "/api/reports",
            {"kind": "lost"},
            extra_headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Cross-origin", body)
        self.assertEqual(self.request_error(
            "/api/auth/login",
            {"password": self.password},
            extra_headers={"Origin": "https://attacker.example"},
        )[0], 403)

    def test_auth_login_is_rate_limited(self):
        for _ in range(web_server.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.request_error(
                "/api/auth/login", {"password": "wrong password"}, "POST",
            )[0], 401)
        status, body = self.request_error(
            "/api/auth/login", {"password": self.password}, "POST",
        )
        self.assertEqual(status, 429)
        self.assertIn("Too many sign-in attempts", body)

    def test_report_creation_requires_unmodified_signed_detection(self):
        photo = base64.b64decode(JPEG)
        token = web_server.detection_token(
            self.server.session_secret,
            photo,
            {"object_class": 39, "color": [1, 2, 3], "features": [0.2] * 5},
        )
        payload = {
            "kind": "lost",
            "object_class": 40,
            "description": "",
            "photo": "data:image/jpeg;base64," + JPEG,
            "color": [1, 2, 3],
            "features": [0.2] * 5,
            "detection_token": token,
        }
        status, body = self.request_error("/api/reports", payload)
        self.assertEqual(status, 400)
        self.assertIn("do not match", body)


if __name__ == "__main__":
    unittest.main()
