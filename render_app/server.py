#!/usr/bin/env python3
"""Local Lost & Found web server with on-device OpenCV DNN inference."""

import argparse
import base64
import binascii
from collections import defaultdict
import hashlib
import hmac
import json
import math
import mimetypes
import os
from pathlib import Path
import sqlite3
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

try:
    import cv2
    import numpy as np
except ImportError as error:
    cv2 = None
    np = None
    OPENCV_IMPORT_ERROR = str(error)
    class OpenCVException(Exception):
        pass

    OPENCV_ERROR = OpenCVException
else:
    OPENCV_IMPORT_ERROR = ""
    OPENCV_ERROR = cv2.error


ROOT = Path(__file__).resolve().parent
DEFAULT_STORE = Path(os.environ.get("DATA_DIR", "/var/data"))
DEFAULT_MODEL = Path(os.environ.get("MODEL_PATH", "/var/data/models/yolov8n.onnx"))
MAX_BODY = 12 * 1024 * 1024
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
PASSWORD_ITERATIONS = 600_000
CONFIDENCE_THRESHOLD = 0.25
NMS_THRESHOLD = 0.45
TILE_SIZE = 800
TILE_OVERLAP = 160
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('lost','found')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','matched')),
    object_class INTEGER NOT NULL CHECK(object_class >= 0),
    object_name TEXT NOT NULL,
    description TEXT NOT NULL,
    photo_path TEXT NOT NULL,
    color_b INTEGER NOT NULL CHECK(color_b BETWEEN 0 AND 255),
    color_g INTEGER NOT NULL CHECK(color_g BETWEEN 0 AND 255),
    color_r INTEGER NOT NULL CHECK(color_r BETWEEN 0 AND 255),
    texture REAL NOT NULL, edge_density REAL NOT NULL,
    saturation REAL NOT NULL, brightness REAL NOT NULL, entropy REAL NOT NULL,
    matched_id INTEGER REFERENCES reports(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK((status = 'open' AND matched_id IS NULL) OR
          (status = 'matched' AND matched_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS reports_open_class_kind
ON reports(status, object_class, kind);
"""


class DetectionUnavailable(RuntimeError):
    """Local model or OpenCV runtime is unavailable."""


def make_password_hash(password):
    if len(password) < 14:
        raise ValueError("Admin password must contain at least 14 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password, encoded_hash):
    try:
        scheme, iterations_text, salt_text, digest_text = encoded_hash.split("$")
        iterations = int(iterations_text)
        if scheme != "pbkdf2_sha256" or not 100_000 <= iterations <= 1_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(
            digest_text + "=" * (-len(digest_text) % 4)
        )
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def valid_password_hash(encoded_hash):
    if not isinstance(encoded_hash, str):
        return False
    try:
        scheme, iterations_text, salt_text, digest_text = encoded_hash.split("$")
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        digest = base64.urlsafe_b64decode(
            digest_text + "=" * (-len(digest_text) % 4)
        )
    except (ValueError, TypeError, binascii.Error):
        return False
    return (
        scheme == "pbkdf2_sha256"
        and 100_000 <= iterations <= 1_000_000
        and len(salt) >= 16
        and len(digest) == 32
    )


def detection_token(secret, photo, detection):
    claims = {
        "photo_sha256": hashlib.sha256(photo).hexdigest(),
        "issued_at": int(time.time()),
        "object_class": detection["object_class"],
        "color": detection["color"],
        "features": detection["features"],
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_detection_token(secret, token, photo, object_class, color, features):
    if not isinstance(token, str) or len(token) > 2048:
        raise ValueError("A valid server detection result is required.")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8"))
    except (ValueError, UnicodeEncodeError, json.JSONDecodeError, binascii.Error) as error:
        raise ValueError("The detection result is invalid or has expired.") from error
    issued_at = claims.get("issued_at")
    if (not isinstance(issued_at, int) or issued_at > int(time.time()) + 10
            or int(time.time()) - issued_at > 30 * 60):
        raise ValueError("The detection result is invalid or has expired.")
    if claims.get("photo_sha256") != hashlib.sha256(photo).hexdigest():
        raise ValueError("The photo changed after server detection. Please analyze it again.")
    if (claims.get("object_class") != object_class
            or claims.get("color") != color
            or claims.get("features") != features):
        raise ValueError("The selected item details do not match the server detection.")


def describe_color(bgr):
    blue, green, red = (float(value) for value in bgr)
    maximum, minimum = max(red, green, blue), min(red, green, blue)
    delta = maximum - minimum
    if maximum < 48:
        return "black"
    if delta < 25:
        return "white" if maximum > 210 else "gray"
    if maximum == red:
        hue = 60 * ((green - blue) / delta % 6)
    elif maximum == green:
        hue = 60 * ((blue - red) / delta + 2)
    else:
        hue = 60 * ((red - green) / delta + 4)
    if hue < 15 or hue >= 345:
        return "red"
    if hue < 45:
        return "orange"
    if hue < 70:
        return "yellow"
    if hue < 165:
        return "green"
    if hue < 200:
        return "teal"
    if hue < 255:
        return "blue"
    if hue < 290:
        return "purple"
    return "magenta"


def analyze_region(region):
    if cv2 is None:
        raise DetectionUnavailable("Install Python OpenCV 4 and NumPy to enable detection.")
    if region is None or region.size == 0:
        raise ValueError("Could not extract the detected item region.")

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    _, deviation = cv2.meanStdDev(laplacian)
    texture = min(1.0, max(0.0, math.log1p(deviation[0][0] ** 2) / 8.0))
    edges = cv2.Canny(gray, 80, 160)
    edge_density = float(cv2.countNonZero(edges)) / edges.size
    hsv_mean = cv2.mean(cv2.cvtColor(region, cv2.COLOR_BGR2HSV))
    saturation = hsv_mean[1] / 255.0
    brightness = hsv_mean[2] / 255.0
    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    probabilities = histogram / gray.size
    nonzero = probabilities[probabilities > 0]
    entropy = float(-np.sum(nonzero * np.log2(nonzero)) / 8.0)

    inset_y, inset_x = region.shape[0] // 6, region.shape[1] // 6
    color_region = region[
        inset_y:max(inset_y + 1, region.shape[0] - inset_y),
        inset_x:max(inset_x + 1, region.shape[1] - inset_x),
    ]
    scale = min(1.0, 100.0 / max(color_region.shape[:2]))
    small = cv2.resize(
        color_region, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
    )
    samples = np.float32(small.reshape((-1, 3)))
    clusters = min(3, len(samples))
    cv2.setRNGSeed(0)
    _, labels, centers = cv2.kmeans(
        samples, clusters, None,
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0),
        2, cv2.KMEANS_PP_CENTERS,
    )
    counts = np.bincount(labels.ravel(), minlength=clusters)
    dominant_bgr = centers[int(np.argmax(counts))]
    color = [int(np.clip(round(value), 0, 255)) for value in dominant_bgr[::-1]]
    features = [
        float(texture), float(edge_density), float(saturation),
        float(brightness), entropy,
    ]
    return color, describe_color(dominant_bgr), features


class OpenCVDNNDetector:
    def __init__(self, model_path):
        if cv2 is None:
            raise DetectionUnavailable(
                "Install Python OpenCV 4 and NumPy to enable local detection."
            ) from ImportError(OPENCV_IMPORT_ERROR)
        if not model_path.is_file():
            raise DetectionUnavailable(f"ONNX model not found: {model_path}")
        try:
            self.net = cv2.dnn.readNetFromONNX(str(model_path))
            if self.net.empty():
                raise DetectionUnavailable(f"Could not load ONNX model: {model_path}")
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except cv2.error as error:
            raise DetectionUnavailable(f"Could not load ONNX model: {error}") from error
        self.lock = threading.Lock()

    @staticmethod
    def tile_origins(length):
        if length <= TILE_SIZE:
            return [0]
        step = TILE_SIZE - TILE_OVERLAP
        starts = list(range(0, length - TILE_SIZE + 1, step))
        final = length - TILE_SIZE
        if starts[-1] != final:
            starts.append(final)
        return starts

    def infer_region(self, image, offset_x=0, offset_y=0):
        height, width = image.shape[:2]
        scale = min(640 / width, 640 / height)
        new_width, new_height = max(1, round(width * scale)), max(1, round(height * scale))
        pad_x, pad_y = (640 - new_width) // 2, (640 - new_height) // 2
        resized = cv2.resize(image, (new_width, new_height))
        letterboxed = np.full((640, 640, 3), 114, dtype=np.uint8)
        letterboxed[pad_y:pad_y + new_height, pad_x:pad_x + new_width] = resized
        blob = cv2.dnn.blobFromImage(
            letterboxed, 1 / 255.0, (640, 640), swapRB=True, crop=False
        )
        try:
            with self.lock:
                self.net.setInput(blob)
                output = self.net.forward()
        except cv2.error as error:
            raise DetectionUnavailable(f"Local YOLO inference failed: {error}") from error

        if output.ndim == 3 and output.shape[1] == 84:
            predictions = output[0].T
        elif output.ndim == 3 and output.shape[2] == 84:
            predictions = output[0]
        elif output.ndim == 2 and output.shape[1] == 84:
            predictions = output
        else:
            raise DetectionUnavailable(
                "Unsupported model output: expected YOLOv8 COCO [1,84,N] or [1,N,84]."
            )

        boxes, scores, classes = [], [], []
        for values in predictions:
            class_scores = values[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            if class_id == 0 or not math.isfinite(confidence) or confidence < CONFIDENCE_THRESHOLD:
                continue
            center_x = (float(values[0]) - pad_x) / scale
            center_y = (float(values[1]) - pad_y) / scale
            box_width, box_height = float(values[2]) / scale, float(values[3]) / scale
            left, top = max(0, int(center_x - box_width / 2)), max(0, int(center_y - box_height / 2))
            right, bottom = min(width, int(center_x + box_width / 2)), min(height, int(center_y + box_height / 2))
            if right <= left or bottom <= top:
                continue
            boxes.append([left + offset_x, top + offset_y, right - left, bottom - top])
            scores.append(confidence)
            classes.append(class_id)
        return boxes, scores, classes

    def detect(self, jpeg):
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Could not decode the JPEG for detection.")
        if image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
            raise ValueError("Photo is too large for local detection (maximum 20 megapixels).")
        height, width = image.shape[:2]
        batches = [self.infer_region(image)]
        if width > TILE_SIZE or height > TILE_SIZE:
            for y in self.tile_origins(height):
                for x in self.tile_origins(width):
                    tile = image[y:min(y + TILE_SIZE, height), x:min(x + TILE_SIZE, width)]
                    batches.append(self.infer_region(tile, x, y))
        boxes = [box for batch, _, _ in batches for box in batch]
        scores = [score for _, batch, _ in batches for score in batch]
        classes = [class_id for _, _, batch in batches for class_id in batch]

        kept = []
        for class_id in set(classes):
            indices = [index for index, value in enumerate(classes) if value == class_id]
            local = cv2.dnn.NMSBoxes(
                [boxes[index] for index in indices],
                [scores[index] for index in indices],
                CONFIDENCE_THRESHOLD,
                NMS_THRESHOLD,
            )
            kept.extend(indices[int(index)] for index in np.asarray(local).reshape(-1))

        results = []
        for index in kept:
            x, y, box_width, box_height = boxes[index]
            color, color_name, features = analyze_region(
                image[y:y + box_height, x:x + box_width]
            )
            results.append({
                "object_class": classes[index],
                "object_name": COCO_CLASSES[classes[index]],
                "confidence": round(scores[index], 4),
                "bbox": [x, y, box_width, box_height],
                "color": color,
                "color_name": color_name,
                "features": features,
            })
        return sorted(results, key=lambda item: item["confidence"], reverse=True)


def open_store(store_dir):
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "photos").mkdir(exist_ok=True)
    db = sqlite3.connect(store_dir / "lost_found.sqlite3", timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(CREATE_TABLES)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 1:
        db.close()
        raise RuntimeError("The SQLite database was created by a newer version of the application.")
    if version == 0:
        db.execute("PRAGMA user_version = 1")
        db.commit()
    return db


def as_report(row):
    rgb = [row["color_r"], row["color_g"], row["color_b"]]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "object_class": row["object_class"],
        "object_name": row["object_name"],
        "description": row["description"],
        "photo_url": f"/photos/{row['id']}",
        "color": rgb,
        "color_name": describe_color((rgb[2], rgb[1], rgb[0])),
        "created_at": row["created_at"],
        "matched_id": row["matched_id"],
    }


def validate_features(features):
    if features is None:
        return (0.0,) * 5
    if (not isinstance(features, list) or len(features) != 5
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in features)):
        raise ValueError("features must contain five numbers from 0 to 1.")
    values = tuple(float(value) for value in features)
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
        raise ValueError("Image features must be in the range [0, 1].")
    return values


def decode_photo(value):
    prefix = "data:image/jpeg;base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("Photo must be a JPEG captured by the camera or selected from this device.")
    try:
        data = base64.b64decode(value[len(prefix):], validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Invalid base64-encoded photo.") from error
    if len(data) < 4 or len(data) > MAX_PHOTO_BYTES:
        raise ValueError("Photo size must be between 4 bytes and 8 MB.")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("The uploaded file does not appear to be a valid JPEG.")
    if cv2 is None:
        raise DetectionUnavailable(
            "The image runtime is unavailable; photo validation cannot run."
        )
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The uploaded JPEG could not be decoded.")
    if image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
        raise ValueError("Photo is too large (maximum 20 megapixels).")
    return data


def similarity(source, candidate):
    source_color = (source["color_b"], source["color_g"], source["color_r"])
    candidate_color = (candidate["color_b"], candidate["color_g"], candidate["color_r"])
    distance = math.sqrt(sum(((a - b) / 255.0) ** 2
                             for a, b in zip(source_color, candidate_color)) / 3.0)
    color_score = max(0.0, min(1.0, 1.0 - distance))
    columns = ("texture", "edge_density", "saturation", "brightness", "entropy")
    feature_score = max(
        0.0, min(1.0, 1.0 - sum(abs(source[key] - candidate[key]) for key in columns) / 5.0)
    )
    description_score = float(
        bool(source["description"])
        and source["description"].strip().casefold()
        == candidate["description"].strip().casefold()
    )
    return 0.55 + 0.25 * color_score + 0.15 * feature_score + 0.05 * description_score


def find_matches(db, report_id, limit=10):
    source = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if source is None:
        raise KeyError("Report not found.")
    if source["status"] != "open" or source["object_class"] == 0:
        return []
    rows = db.execute(
        """SELECT * FROM reports
           WHERE status='open' AND object_class > 0
             AND object_class=? AND kind != ?
           ORDER BY id DESC""",
        (source["object_class"], source["kind"]),
    ).fetchall()
    ranked = sorted(rows, key=lambda row: (similarity(source, row), row["id"]), reverse=True)
    return [{"report": as_report(row), "score": similarity(source, row)}
            for row in ranked[:limit]]


class LostFoundHandler(BaseHTTPRequestHandler):
    server_version = "FoundRender/1.0"

    @property
    def store_dir(self):
        return self.server.store_dir

    def log_message(self, message, *args):
        print(f"[local-api] {self.address_string()} {message % args}")

    def send_json(self, status, value, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        if self.server.secure_cookies:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if headers:
            for name, value in headers:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def require_same_origin(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not origin or not host:
            self.send_json(403, {"error": "A same-origin browser request is required."})
            return True
        parsed = urlparse(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.netloc.casefold() != host.casefold()):
            self.send_json(403, {"error": "Cross-origin requests are not allowed."})
            return True
        return False

    def current_session(self):
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(self.server.cookie_name)
        if morsel is None:
            return None
        try:
            session_id, expiry_text, signature = morsel.value.split(".", 2)
            expiry = int(expiry_text)
            signed = f"{session_id}.{expiry_text}".encode("ascii")
            expected = hmac.new(
                self.server.session_secret, signed, hashlib.sha256
            ).hexdigest()
            if expiry <= int(time.time()) or not hmac.compare_digest(signature, expected):
                return None
        except (ValueError, UnicodeEncodeError):
            return None
        with self.server.auth_lock:
            session = self.server.sessions.get(session_id)
            if not session or session["expires"] != expiry or expiry <= int(time.time()):
                self.server.sessions.pop(session_id, None)
                return None
            return {"id": session_id, "csrf": session["csrf"], "expires": expiry}

    def require_admin(self, csrf=False):
        session = self.current_session()
        if session is None:
            self.send_json(401, {"error": "Admin sign-in is required."})
            return None
        if csrf:
            token = self.headers.get("X-CSRF-Token", "")
            if not token or not hmac.compare_digest(token, session["csrf"]):
                self.send_json(403, {"error": "The security token is missing or expired. Reload the page."})
                return None
        return session

    def session_cookie(self, session_id, expiry):
        signed = f"{session_id}.{expiry}".encode("ascii")
        signature = hmac.new(
            self.server.session_secret, signed, hashlib.sha256
        ).hexdigest()
        value = f"{session_id}.{expiry}.{signature}"
        secure = "; Secure" if self.server.secure_cookies else ""
        return (
            f"{self.server.cookie_name}={value}; Path=/; HttpOnly; "
            f"SameSite=Strict; Max-Age={SESSION_SECONDS}{secure}"
        )

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request size.") from error
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Invalid or oversized request.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid JSON request.") from error
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            if parsed.path == "/api/auth":
                session = self.current_session()
                return self.send_json(200, {
                    "is_admin": session is not None,
                    "csrf_token": session["csrf"] if session else "",
                })
            if parsed.path == "/api/health":
                db = open_store(self.store_dir)
                try:
                    count = db.execute(
                        "SELECT COUNT(*) FROM reports WHERE status='open' AND object_class > 0"
                    ).fetchone()[0]
                finally:
                    db.close()
                try:
                    self.server.get_detector()
                    detection = {"available": True, "error": ""}
                except DetectionUnavailable as error:
                    detection = {"available": False, "error": str(error)}
                return self.send_json(200, {
                    "ok": True, "open_reports": count, "coco_classes": 80,
                    "available_categories": 79, "detection": detection,
                })
            if parsed.path == "/api/classes":
                return self.send_json(200, [
                    {"id": index, "name": name}
                    for index, name in enumerate(COCO_CLASSES) if index != 0
                ])
            if parsed.path == "/api/reports":
                db = open_store(self.store_dir)
                try:
                    rows = db.execute(
                        """SELECT * FROM reports WHERE object_class > 0
                           ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC"""
                    ).fetchall()
                finally:
                    db.close()
                return self.send_json(200, [as_report(row) for row in rows])
            if len(parts) == 3 and parts[:2] == ["api", "matches"]:
                db = open_store(self.store_dir)
                try:
                    return self.send_json(200, find_matches(db, int(parts[2])))
                finally:
                    db.close()
            if len(parts) == 2 and parts[0] == "photos":
                db = open_store(self.store_dir)
                try:
                    row = db.execute(
                        "SELECT photo_path FROM reports WHERE id=? AND object_class > 0",
                        (int(parts[1]),),
                    ).fetchone()
                finally:
                    db.close()
                if row is None:
                    return self.send_json(404, {"error": "Photo not found."})
                path = Path(row["photo_path"]).resolve()
                photos_root = (self.store_dir / "photos").resolve()
                if path.parent != photos_root or not path.is_file():
                    return self.send_json(404, {"error": "Photo not found."})
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=60")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                if self.server.secure_cookies:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
                self.end_headers()
                return self.wfile.write(data)
            if parsed.path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if parsed.path in ("/app.js", "/styles.css"):
                return self.serve_static(parts[-1])
            return self.send_json(404, {"error": "Route not found."})
        except (ValueError, KeyError, sqlite3.Error, OSError) as error:
            return self.send_json(400, {"error": str(error)})

    def serve_static(self, filename):
        path = (ROOT / filename).resolve()
        if path.parent != ROOT or not path.is_file():
            return self.send_json(404, {"error": "File not found."})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or
                         "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if self.server.secure_cookies:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            origin_error = self.require_same_origin()
            if origin_error:
                return
            if parsed.path == "/api/auth/login":
                client_ip = self.client_address[0]
                retry_after = self.server.login_retry_after(client_ip)
                if retry_after:
                    return self.send_json(
                        429,
                        {"error": "Too many sign-in attempts. Try again later."},
                        [("Retry-After", str(retry_after))],
                    )
                payload = self.read_json()
                password = payload.get("password")
                if (not isinstance(password, str) or len(password) > 256
                        or not verify_password(
                            password, self.server.admin_password_hash
                        )):
                    self.server.record_login_failure(client_ip)
                    return self.send_json(
                        401, {"error": "Incorrect admin password."}
                    )
                self.server.clear_login_failures(client_ip)
                session_id = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                expiry = int(time.time()) + SESSION_SECONDS
                with self.server.auth_lock:
                    self.server.sessions[session_id] = {
                        "csrf": csrf_token, "expires": expiry,
                    }
                return self.send_json(200, {
                    "is_admin": True, "csrf_token": csrf_token,
                }, [("Set-Cookie", self.session_cookie(session_id, expiry))])
            if parsed.path == "/api/auth/logout":
                session = self.require_admin(csrf=True)
                if session is None:
                    return
                with self.server.auth_lock:
                    self.server.sessions.pop(session["id"], None)
                secure = "; Secure" if self.server.secure_cookies else ""
                return self.send_json(200, {"ok": True}, [(
                    "Set-Cookie",
                    f"{self.server.cookie_name}=; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age=0{secure}",
                )])
            payload = self.read_json()
            if parsed.path == "/api/detect":
                photo = decode_photo(payload.get("photo"))
                detections = self.server.get_detector().detect(photo)
                for detection in detections:
                    detection["detection_token"] = detection_token(
                        self.server.session_secret, photo, detection
                    )
                return self.send_json(200, {"detections": detections})
            if parsed.path == "/api/reports":
                return self.create_report(payload)
            if parsed.path == "/api/matches/confirm":
                if self.require_admin(csrf=True) is None:
                    return
                return self.confirm_match(payload)
            return self.send_json(404, {"error": "Route not found."})
        except DetectionUnavailable as error:
            return self.send_json(503, {"error": str(error)})
        except OPENCV_ERROR as error:
            return self.send_json(400, {"error": f"Could not process photo: {error}"})
        except (ValueError, KeyError, sqlite3.Error, OSError) as error:
            return self.send_json(400, {"error": str(error)})

    def do_PUT(self):
        parsed = urlparse(self.path)
        if self.require_same_origin():
            return
        if self.require_admin(csrf=True) is None:
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) != 3 or parts[:2] != ["api", "reports"]:
            return self.send_json(404, {"error": "Route not found."})
        try:
            report_id = int(parts[2])
            if report_id <= 0:
                raise ValueError
        except ValueError:
            return self.send_json(400, {"error": "Report ID must be a positive integer."})
        try:
            return self.update_report(report_id, self.read_json())
        except (ValueError, sqlite3.Error, OSError) as error:
            return self.send_json(400, {"error": str(error)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if self.require_same_origin():
            return
        if self.require_admin(csrf=True) is None:
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) != 3 or parts[:2] != ["api", "reports"]:
            return self.send_json(404, {"error": "Route not found."})
        try:
            report_id = int(parts[2])
            if report_id <= 0:
                raise ValueError
        except ValueError:
            return self.send_json(400, {"error": "Report ID must be a positive integer."})
        try:
            return self.delete_report(report_id)
        except (sqlite3.Error, OSError) as error:
            return self.send_json(400, {"error": str(error)})

    def update_report(self, report_id, payload):
        if set(payload) - {"kind", "description"}:
            raise ValueError("Only report type and description can be edited.")
        kind = payload.get("kind")
        description = payload.get("description")
        if kind not in ("lost", "found"):
            raise ValueError("kind must be lost or found.")
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError("Description must be a string no longer than 1,000 characters.")

        db = open_store(self.store_dir)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM reports WHERE id=?", (report_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                return self.send_json(404, {"error": "Report not found."})
            if row["status"] != "open":
                db.rollback()
                return self.send_json(
                    409, {"error": "Matched reports cannot be edited individually."}
                )
            db.execute(
                "UPDATE reports SET kind=?, description=? WHERE id=?",
                (kind, description, report_id),
            )
            db.commit()
            updated = db.execute(
                "SELECT * FROM reports WHERE id=?", (report_id,)
            ).fetchone()
            return self.send_json(200, {
                "report": as_report(updated),
                "matches": find_matches(db, report_id),
            })
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete_report(self, report_id):
        db = open_store(self.store_dir)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, photo_path FROM reports WHERE id=?", (report_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                return self.send_json(404, {"error": "Report not found."})
            if row["status"] != "open":
                db.rollback()
                return self.send_json(
                    409, {"error": "Matched reports cannot be deleted individually."}
                )
            photo_path = row["photo_path"]
            db.execute("DELETE FROM reports WHERE id=?", (report_id,))
            photo_resolved = Path(photo_path).resolve()
            remaining_paths = db.execute(
                "SELECT photo_path FROM reports"
            ).fetchall()
            photo_in_use = any(
                Path(remaining["photo_path"]).resolve() == photo_resolved
                for remaining in remaining_paths
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        photo_removed = False
        cleanup_warning = ""
        if not photo_in_use:
            path = Path(photo_path).resolve()
            photos_root = (self.store_dir / "photos").resolve()
            if path.parent == photos_root:
                try:
                    path.unlink(missing_ok=True)
                    photo_removed = True
                except OSError:
                    cleanup_warning = (
                        "The report was deleted, but its photo could not be removed from disk."
                    )
            else:
                cleanup_warning = (
                    "The report was deleted, but its photo is outside the managed photo folder "
                    "and was left untouched."
                )
        return self.send_json(200, {
            "deleted": True,
            "photo_removed": photo_removed,
            "photo_in_use": photo_in_use,
            "photo_cleanup_warning": cleanup_warning,
        })

    def create_report(self, payload):
        kind = payload.get("kind")
        class_id = payload.get("object_class")
        if kind not in ("lost", "found"):
            raise ValueError("kind must be lost or found.")
        if type(class_id) is not int or class_id <= 0 or class_id >= len(COCO_CLASSES):
            raise ValueError("Choose a valid item class other than person.")
        description = payload.get("description", "")
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError("Description must be a string no longer than 1,000 characters.")
        photo = decode_photo(payload.get("photo"))
        color = payload.get("color")
        if (not isinstance(color, list) or len(color) != 3
                or any(type(value) is not int or value < 0 or value > 255 for value in color)):
            raise ValueError("color must be an RGB array of three values from 0 to 255.")
        features = validate_features(payload.get("features"))
        verify_detection_token(
            self.server.session_secret,
            payload.get("detection_token"),
            photo,
            class_id,
            color,
            list(features),
        )
        db = open_store(self.store_dir)
        photo_path = None
        try:
            cursor = db.execute(
                """INSERT INTO reports(
                   kind, object_class, object_name, description, photo_path,
                   color_b, color_g, color_r, texture, edge_density,
                   saturation, brightness, entropy)
                   VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, class_id, COCO_CLASSES[class_id], description,
                 color[2], color[1], color[0], *features),
            )
            report_id = cursor.lastrowid
            photo_path = self.store_dir / "photos" / f"item-{report_id}.jpg"
            with photo_path.open("xb") as image_file:
                image_file.write(photo)
            db.execute("UPDATE reports SET photo_path=? WHERE id=?",
                       (str(photo_path.resolve()), report_id))
            db.commit()
        except Exception:
            db.rollback()
            if photo_path is not None:
                try:
                    photo_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            db.close()

        db = open_store(self.store_dir)
        try:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            return self.send_json(
                201, {"report": as_report(row), "matches": find_matches(db, report_id)}
            )
        finally:
            db.close()

    def confirm_match(self, payload):
        try:
            first_id = int(payload["report_id"])
            second_id = int(payload["candidate_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Provide report_id and candidate_id.") from error
        if first_id == second_id:
            raise ValueError("A report cannot be matched with itself.")
        db = open_store(self.store_dir)
        try:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute(
                "SELECT kind, status, object_class FROM reports WHERE id=?", (first_id,)
            ).fetchone()
            second = db.execute(
                "SELECT kind, status, object_class FROM reports WHERE id=?", (second_id,)
            ).fetchone()
            if first is None or second is None:
                raise ValueError("One of the reports was not found.")
            if (first["status"] != "open" or second["status"] != "open"
                    or first["kind"] == second["kind"] or first["object_class"] <= 0
                    or second["object_class"] <= 0
                    or first["object_class"] != second["object_class"]):
                raise ValueError(
                    "A match must pair open reports of opposite types for the same item class, "
                    "excluding person."
                )
            db.execute("UPDATE reports SET status='matched', matched_id=? WHERE id=?",
                       (second_id, first_id))
            db.execute("UPDATE reports SET status='matched', matched_id=? WHERE id=?",
                       (first_id, second_id))
            db.commit()
            return self.send_json(200, {"ok": True, "closed_ids": [first_id, second_id]})
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    cookie_name = "found-admin"

    def __init__(
        self, address, store_dir, model_path=DEFAULT_MODEL,
        admin_password_hash=None, session_secret=None, secure_cookies=False,
    ):
        if not valid_password_hash(admin_password_hash):
            raise ValueError("A valid ADMIN_PASSWORD_HASH is required.")
        if not session_secret or len(session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 bytes.")
        self.store_dir = store_dir.resolve()
        self.model_path = Path(model_path).expanduser().resolve()
        self.admin_password_hash = admin_password_hash
        self.session_secret = session_secret
        self.secure_cookies = secure_cookies
        self.sessions = {}
        self.login_attempts = defaultdict(list)
        self.auth_lock = threading.Lock()
        self._detector = None
        self._detector_error = None
        self._detector_lock = threading.Lock()
        super().__init__(address, LostFoundHandler)

    def login_retry_after(self, client_ip):
        now = time.monotonic()
        with self.auth_lock:
            attempts = [
                attempt for attempt in self.login_attempts[client_ip]
                if now - attempt < LOGIN_WINDOW_SECONDS
            ]
            self.login_attempts[client_ip] = attempts
            if len(attempts) < LOGIN_MAX_ATTEMPTS:
                return 0
            return max(1, math.ceil(LOGIN_WINDOW_SECONDS - (now - attempts[0])))

    def record_login_failure(self, client_ip):
        now = time.monotonic()
        with self.auth_lock:
            attempts = [
                attempt for attempt in self.login_attempts[client_ip]
                if now - attempt < LOGIN_WINDOW_SECONDS
            ]
            attempts.append(now)
            self.login_attempts[client_ip] = attempts

    def clear_login_failures(self, client_ip):
        with self.auth_lock:
            self.login_attempts.pop(client_ip, None)

    def get_detector(self):
        with self._detector_lock:
            if self._detector_error is not None:
                raise self._detector_error
            if self._detector is None:
                try:
                    self._detector = OpenCVDNNDetector(self.model_path)
                except DetectionUnavailable as error:
                    self._detector_error = error
                    raise
            return self._detector


def main():
    parser = argparse.ArgumentParser(description="Render Lost & Found website")
    parser.add_argument("--hash-password", action="store_true")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "10000")))
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()
    if args.hash_password:
        import getpass
        first = getpass.getpass("New admin password (14+ characters): ")
        second = getpass.getpass("Confirm admin password: ")
        if first != second:
            raise SystemExit("Passwords do not match.")
        try:
            print(make_password_hash(first))
        except ValueError as error:
            raise SystemExit(str(error)) from error
        return
    admin_password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    session_secret = os.environ.get("SESSION_SECRET", "").encode("utf-8")
    if not admin_password_hash or not session_secret:
        raise SystemExit(
            "ADMIN_PASSWORD_HASH and SESSION_SECRET must be configured before startup."
        )
    secure_cookie_setting = os.environ.get("COOKIE_SECURE", os.environ.get("RENDER", ""))
    secure_cookies = secure_cookie_setting.casefold() in ("1", "true", "yes")
    store_dir = args.store.expanduser().resolve()
    db = open_store(store_dir)
    db.close()
    try:
        server = LocalServer(
            (args.host, args.port), store_dir, args.model,
            admin_password_hash=admin_password_hash,
            session_secret=session_secret,
            secure_cookies=secure_cookies,
        )
    except OSError as error:
        if error.errno in (48, 98, 10048):
            raise SystemExit(
                f"Port {args.port} is already in use. Stop the other local server "
                f"or start the site with --port {args.port + 1}."
            ) from error
        raise
    print(f"Found Render listening on {args.host}:{args.port}")
    print(f"Persistent data directory: {store_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
