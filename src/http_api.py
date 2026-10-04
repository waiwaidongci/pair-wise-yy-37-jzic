from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path == "/api/inspectors":
                    actor, role = self._identity()
                    self._json(200, {"inspectors": service.list_inspectors(role)})
                elif path.startswith("/api/schedule/"):
                    day = path.rsplit("/", 1)[-1]
                    actor, role = self._identity()
                    self._json(200, service.day_schedule(day, role))
                elif path == "/api/schedule":
                    actor, role = self._identity()
                    self._json(200, service.day_schedule(None, role))
                elif path.startswith("/api/batches/"):
                    parts = path.strip("/").split("/")
                    batch_id = int(parts[2])
                    actor, role = self._identity()
                    self._json(200, service.get_batch(batch_id, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/inspectors":
                    self._json(201, service.create_inspector(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records") and path.count("/") == 4:
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif "/records/" in path and path.endswith("/close"):
                    parts = path.strip("/").split("/")
                    record_id = int(parts[2] if parts[1] == "records" else parts[3])
                    self._json(200, service.close_record(record_id, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path.startswith("/api/schedule/") and path.endswith("/build"):
                    day = path.split("/")[3]
                    self._json(200, service.build_day_queue(day, actor, role))
                elif path == "/api/schedule/build":
                    self._json(200, service.build_day_queue(None, actor, role))
                elif path == "/api/schedule/dispatch-next":
                    self._json(200, service.dispatch_next(None, actor, role))
                elif path.startswith("/api/schedule/") and path.endswith("/dispatch-next"):
                    day = path.split("/")[3]
                    self._json(200, service.dispatch_next(day, actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/claim-next"):
                    day = body.get("day")
                    inspector_id = body.get("inspector_id")
                    self._json(200, service.claim_next(
                        day, inspector_id, actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/claim"):
                    batch_id = int(path.split("/")[3])
                    self._json(200, service.claim_batch(
                        batch_id, body.get("inspector_id"), actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/execute"):
                    batch_id = int(path.split("/")[3])
                    self._json(200, service.execute_batch(
                        batch_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
