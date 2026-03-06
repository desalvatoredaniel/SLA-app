from __future__ import annotations

import ast
import json
import math
import os
import re
import smtplib
import ssl
import threading
from base64 import b64encode
from copy import deepcopy
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib import error as urllib_error
from urllib.parse import urlparse
from urllib import request as urllib_request
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

ALLOWED_HTTP_METHODS = {"GET", "HEAD"}
ALLOWED_AUTH_TYPES = {"none", "basic", "bearer"}
ENV_LINE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
SERVER_HEALTH_CONFIG_PATH = Path(app.instance_path) / "server_health_checks.json"
ENV_PATH = Path(os.getenv("SLA_APP_ENV_PATH", ".env"))

try:
    HEALTH_CHECK_INTERVAL_SECONDS = max(2.0, min(300.0, float(os.getenv("SLA_HEALTH_CHECK_INTERVAL_SECONDS", "15"))))
except ValueError:
    HEALTH_CHECK_INTERVAL_SECONDS = 15.0

SERVER_HEALTH_LOCK = threading.RLock()
_health_checker_thread: threading.Thread | None = None
_health_checker_start_lock = threading.Lock()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


try:
    ALERT_REMINDER_SECONDS = max(60.0, min(86_400.0, float(os.getenv("SLA_ALERT_REMINDER_SECONDS", "900"))))
except ValueError:
    ALERT_REMINDER_SECONDS = 900.0

SMTP_HOST = os.getenv("SLA_ALERT_SMTP_HOST", "").strip()
try:
    SMTP_PORT = int(os.getenv("SLA_ALERT_SMTP_PORT", "587"))
except ValueError:
    SMTP_PORT = 587
SMTP_USERNAME = os.getenv("SLA_ALERT_SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SLA_ALERT_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SLA_ALERT_FROM", "").strip()
SMTP_USE_TLS = _env_bool("SLA_ALERT_SMTP_USE_TLS", True)
SMTP_USE_SSL = _env_bool("SLA_ALERT_SMTP_USE_SSL", False)
EMAIL_SUBJECT_PREFIX = os.getenv("SLA_ALERT_SUBJECT_PREFIX", "[SLA Server Health]").strip() or "[SLA Server Health]"

SERVER_GROUP_OPTIONS = (
    "LEAP BO PROD",
    "LEAP BO STAGE",
    "LEAP BO QA",
    "PORTAL PROD",
    "PORTAL STAGE",
    "PORTAL QA",
)
SERVER_GROUP_DEFAULT = "LEAP BO PROD"

RELEASES: list[dict[str, Any]] = [
    {
        "id": "1",
        "version": "v2.8.1",
        "name": "Performance Optimization",
        "status": "deployed",
        "environment": "production",
        "deployed_by": "Sarah Chen",
        "deployed_at": "2026-03-06 14:32",
        "services": 8,
        "commits": 24,
    },
    {
        "id": "2",
        "version": "v2.8.2",
        "name": "Security Patches",
        "status": "in-progress",
        "environment": "staging",
        "deployed_by": "Mike Johnson",
        "deployed_at": "2026-03-06 15:15",
        "services": 5,
        "commits": 12,
    },
    {
        "id": "3",
        "version": "v2.9.0",
        "name": "Feature: Advanced Analytics",
        "status": "scheduled",
        "environment": "development",
        "deployed_by": "Auto Deploy",
        "deployed_at": "2026-03-07 09:00",
        "services": 12,
        "commits": 47,
    },
    {
        "id": "4",
        "version": "v2.7.9",
        "name": "Hotfix: API Gateway",
        "status": "deployed",
        "environment": "production",
        "deployed_by": "Alex Rodriguez",
        "deployed_at": "2026-03-05 22:10",
        "services": 3,
        "commits": 5,
    },
    {
        "id": "5",
        "version": "v2.8.0",
        "name": "Database Migration",
        "status": "failed",
        "environment": "staging",
        "deployed_by": "System",
        "deployed_at": "2026-03-06 11:45",
        "services": 6,
        "commits": 18,
    },
]

SLA_PAYMENTS_INITIAL: list[dict[str, Any]] = [
    {
        "id": "SLA-2026-001",
        "customer": "Acme Corporation",
        "amount": 15000,
        "reason": "API Gateway Outage",
        "status": "pending",
        "incident_id": "INC-8372",
        "downtime": 45,
        "sla_violation": "99.9% uptime breach",
        "submitted_at": "2026-03-06 08:15",
    },
    {
        "id": "SLA-2026-002",
        "customer": "TechStart Inc",
        "amount": 8500,
        "reason": "Database Latency",
        "status": "processing",
        "incident_id": "INC-8371",
        "downtime": 28,
        "sla_violation": "Response time > 200ms",
        "submitted_at": "2026-03-05 16:42",
    },
    {
        "id": "SLA-2026-003",
        "customer": "Global Finance Ltd",
        "amount": 42000,
        "reason": "Complete Service Outage",
        "status": "completed",
        "incident_id": "INC-8365",
        "downtime": 120,
        "sla_violation": "Critical service unavailable",
        "submitted_at": "2026-03-04 11:20",
    },
    {
        "id": "SLA-2026-004",
        "customer": "DataCorp Solutions",
        "amount": 6200,
        "reason": "Authentication Service Delay",
        "status": "failed",
        "incident_id": "INC-8380",
        "downtime": 15,
        "sla_violation": "Auth response > 100ms",
        "submitted_at": "2026-03-06 13:05",
    },
    {
        "id": "SLA-2026-005",
        "customer": "CloudNet Systems",
        "amount": 11000,
        "reason": "CDN Performance Issues",
        "status": "processing",
        "incident_id": "INC-8375",
        "downtime": 35,
        "sla_violation": "CDN latency breach",
        "submitted_at": "2026-03-05 21:30",
    },
]

sla_payments: list[dict[str, Any]] = deepcopy(SLA_PAYMENTS_INITIAL)


def _ensure_instance_dir() -> None:
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    try:
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, str):
                return parsed
        except (ValueError, SyntaxError):
            return value[1:-1]

    return value


def _read_env_map() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_env_lines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ENV_LINE_PATTERN.match(line)
        if not match:
            continue
        key = match.group(1)
        _, raw_value = line.split("=", 1)
        values[key] = _parse_env_value(raw_value)
    return values


def _write_env_lines(lines: list[str]) -> None:
    if ENV_PATH.parent != Path("."):
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _upsert_env_value(key: str, value: str) -> None:
    if not key:
        return

    serialized = json.dumps(value)
    replacement = f"{key}={serialized}"
    lines = _read_env_lines()

    replaced = False
    updated_lines: list[str] = []
    for line in lines:
        match = ENV_LINE_PATTERN.match(line)
        if match and match.group(1) == key:
            updated_lines.append(replacement)
            replaced = True
        else:
            updated_lines.append(line)

    if not replaced:
        if updated_lines and updated_lines[-1].strip() != "":
            updated_lines.append("")
        updated_lines.append(replacement)

    _write_env_lines(updated_lines)


def _delete_env_value(key: str) -> None:
    if not key:
        return

    lines = _read_env_lines()
    updated_lines = [line for line in lines if not (ENV_LINE_PATTERN.match(line) and ENV_LINE_PATTERN.match(line).group(1) == key)]

    if updated_lines != lines:
        _write_env_lines(updated_lines)


def _secret_from_env(env_key: str) -> str:
    if not env_key:
        return ""

    runtime_value = os.getenv(env_key)
    if runtime_value:
        return runtime_value

    return _read_env_map().get(env_key, "")


def _has_secret(env_key: str) -> bool:
    return bool(_secret_from_env(env_key))


def _secret_key_for(check_id: str, suffix: str) -> str:
    sanitized_id = re.sub(r"[^A-Za-z0-9]", "_", check_id).upper()
    return f"SLA_SERVER_HEALTH_{sanitized_id}_{suffix}"


def _normalize_server_group(raw: str | None) -> str:
    if not raw:
        return SERVER_GROUP_DEFAULT
    normalized = re.sub(r"\s+", " ", str(raw).upper()).strip()
    if normalized in SERVER_GROUP_OPTIONS:
        return normalized
    return SERVER_GROUP_DEFAULT


def _is_valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _derive_name_from_url(value: str, *, fallback_index: int) -> str:
    parsed = urlparse(value)
    host = parsed.netloc or f"Server {fallback_index}"
    path = parsed.path.strip("/")
    if path:
        first_segment = path.split("/")[0]
        return f"{host}/{first_segment}"[:80]
    return host[:80]


def _parse_bulk_line(line: str, *, fallback_group: str, fallback_index: int) -> tuple[str, str, str] | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    parts = [part.strip() for part in raw.split(",")]

    if len(parts) >= 2:
        left, right = parts[0], parts[1]
        if _is_valid_http_url(left) and not _is_valid_http_url(right):
            url = left
            name = right or _derive_name_from_url(url, fallback_index=fallback_index)
        else:
            name = left
            url = right
        group = _normalize_server_group(parts[2] if len(parts) >= 3 else fallback_group)
    else:
        url = raw
        name = _derive_name_from_url(url, fallback_index=fallback_index)
        group = fallback_group

    if not _is_valid_http_url(url):
        return None

    if not name:
        name = _derive_name_from_url(url, fallback_index=fallback_index)

    return name[:80], url, group


def _parse_recipients(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,\n;]+", raw)
    recipients = [part.strip() for part in parts if part.strip()]
    return recipients


def _smtp_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def _send_alert_email(subject: str, body: str, recipients: list[str]) -> tuple[bool, str]:
    if not recipients:
        return False, "No recipients configured"
    if not _smtp_is_configured():
        return False, "SMTP is not configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.ehlo()
                if SMTP_USE_TLS:
                    smtp.starttls()
                    smtp.ehlo()
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    return True, ""


def _normalize_server_health_check(raw: dict[str, Any]) -> dict[str, Any]:
    method = str(raw.get("method", "GET")).upper()
    auth_type = str(raw.get("auth_type", "none")).lower()
    if method not in ALLOWED_HTTP_METHODS:
        method = "GET"
    if auth_type not in ALLOWED_AUTH_TYPES:
        auth_type = "none"

    last_check = raw.get("last_check")
    if not isinstance(last_check, dict):
        last_check = None

    check_id = str(raw.get("id") or uuid4().hex)

    return {
        "id": check_id,
        "name": str(raw.get("name") or "Unnamed Check").strip(),
        "server_group": _normalize_server_group(str(raw.get("server_group") or "")),
        "url": str(raw.get("url") or "").strip(),
        "method": method,
        "auth_type": auth_type,
        "username": str(raw.get("username") or "").strip(),
        "password_env_key": str(raw.get("password_env_key") or "").strip(),
        "bearer_token_env_key": str(raw.get("bearer_token_env_key") or "").strip(),
        "timeout_seconds": _coerce_float(raw.get("timeout_seconds"), 5.0, 1.0, 30.0),
        "expected_status": _coerce_int(raw.get("expected_status"), 200, 100, 599),
        "verify_tls": bool(raw.get("verify_tls", True)),
        "is_enabled": bool(raw.get("is_enabled", True)),
        "email_alerts_enabled": bool(raw.get("email_alerts_enabled", False)),
        "alert_recipients": str(raw.get("alert_recipients") or "").strip(),
        "alert_on_recovery": bool(raw.get("alert_on_recovery", True)),
        "last_alert": raw.get("last_alert") if isinstance(raw.get("last_alert"), dict) else None,
        "last_check": last_check,
        "total_checks": _coerce_int(raw.get("total_checks"), 0, 0, 10_000_000),
        "successful_checks": _coerce_int(raw.get("successful_checks"), 0, 0, 10_000_000),
    }


def _load_server_health_checks() -> list[dict[str, Any]]:
    _ensure_instance_dir()
    if not SERVER_HEALTH_CONFIG_PATH.exists():
        return []

    try:
        payload = json.loads(SERVER_HEALTH_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(payload, list):
        return []

    checks: list[dict[str, Any]] = []
    migrated = False

    for item in payload:
        if not isinstance(item, dict):
            continue

        normalized = _normalize_server_health_check(item)
        if not normalized["url"]:
            continue

        legacy_password = str(item.get("password") or "")
        if legacy_password:
            if not normalized["password_env_key"]:
                normalized["password_env_key"] = _secret_key_for(normalized["id"], "PASSWORD")
            if not _has_secret(normalized["password_env_key"]):
                _upsert_env_value(normalized["password_env_key"], legacy_password)
            migrated = True

        legacy_bearer = str(item.get("bearer_token") or "")
        if legacy_bearer:
            if not normalized["bearer_token_env_key"]:
                normalized["bearer_token_env_key"] = _secret_key_for(normalized["id"], "BEARER_TOKEN")
            if not _has_secret(normalized["bearer_token_env_key"]):
                _upsert_env_value(normalized["bearer_token_env_key"], legacy_bearer)
            migrated = True

        checks.append(normalized)

    if migrated:
        SERVER_HEALTH_CONFIG_PATH.write_text(json.dumps(checks, indent=2), encoding="utf-8")

    return checks


def _save_server_health_checks() -> None:
    _ensure_instance_dir()
    with SERVER_HEALTH_LOCK:
        payload = json.dumps(server_health_checks, indent=2)
    SERVER_HEALTH_CONFIG_PATH.write_text(payload, encoding="utf-8")


def _find_server_health_check(check_id: str) -> tuple[int, dict[str, Any] | None]:
    for index, check in enumerate(server_health_checks):
        if check["id"] == check_id:
            return index, check
    return -1, None


def _notice_text(notice_code: str | None, *, added: str | None = None, skipped: str | None = None) -> str:
    if not notice_code:
        return ""

    try:
        added_count = int(added) if added is not None else 0
    except ValueError:
        added_count = 0
    try:
        skipped_count = int(skipped) if skipped is not None else 0
    except ValueError:
        skipped_count = 0

    notices = {
        "added": "Health check target added.",
        "updated": "Health check target updated.",
        "deleted": "Health check target removed.",
        "bulk-empty": "Bulk upload is empty.",
        "bulk-invalid-alerts": "Bulk upload requires alert recipients when email alerts are enabled.",
        "tested-up": "Health check passed.",
        "tested-down": "Health check failed.",
        "tested-all": "All enabled checks were tested.",
        "missing-required": "Required fields are missing (including credentials for the selected auth type).",
        "missing-target": "Health check target was not found.",
    }
    if notice_code == "bulk-added":
        return f"Bulk upload complete. Added {added_count} check(s), skipped {skipped_count}."

    return notices.get(notice_code, "")


def _server_health_stats() -> dict[str, int]:
    with SERVER_HEALTH_LOCK:
        checks = [deepcopy(check) for check in server_health_checks]

    enabled = [check for check in checks if check["is_enabled"]]
    up = 0
    down = 0

    for check in enabled:
        last_check = check.get("last_check") or {}
        if last_check.get("is_up") is True:
            up += 1
        elif last_check:
            down += 1

    return {
        "configured": len(checks),
        "enabled": len(enabled),
        "up": up,
        "down": down,
    }


def _apply_check_result(check: dict[str, Any], result: dict[str, Any]) -> None:
    check["last_check"] = result
    check["total_checks"] = int(check.get("total_checks") or 0) + 1
    if result.get("is_up"):
        check["successful_checks"] = int(check.get("successful_checks") or 0) + 1


def _parse_checked_at(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _format_last_ping_display(value: str | None) -> str:
    checked_at = _parse_checked_at(value)
    if checked_at is None:
        return "never"
    return checked_at.strftime("%Y-%m-%d %H:%M:%S UTC")


def _check_is_stale(check: dict[str, Any], *, max_age_seconds: int) -> bool:
    last_check = check.get("last_check")
    if not isinstance(last_check, dict):
        return True

    checked_at = _parse_checked_at(last_check.get("checked_at"))
    if checked_at is None:
        return True

    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
    return age_seconds >= max_age_seconds


def _refresh_enabled_server_health_checks(*, force: bool = False, max_age_seconds: int = 45) -> None:
    with SERVER_HEALTH_LOCK:
        checks_to_run = [
            {"id": check["id"], "snapshot": dict(check)}
            for check in server_health_checks
            if check.get("is_enabled")
            and (force or _check_is_stale(check, max_age_seconds=max_age_seconds))
        ]

    if not checks_to_run:
        return

    updated = False
    for item in checks_to_run:
        result = _run_server_health_check(item["snapshot"])
        alert_context: dict[str, Any] | None = None
        previous_last_check: dict[str, Any] | None = None
        with SERVER_HEALTH_LOCK:
            _, live_check = _find_server_health_check(item["id"])
            if live_check is None or not live_check.get("is_enabled"):
                continue
            previous_last_check = deepcopy(live_check.get("last_check")) if isinstance(live_check.get("last_check"), dict) else None
            _apply_check_result(live_check, result)
            alert_context = deepcopy(live_check)
            updated = True

        if alert_context is not None:
            alert_result = _evaluate_and_send_alert(alert_context, previous_last_check, result)
            if alert_result is not None:
                with SERVER_HEALTH_LOCK:
                    _, target_check = _find_server_health_check(item["id"])
                    if target_check is not None:
                        target_check["last_alert"] = alert_result
                        updated = True

    if updated:
        _save_server_health_checks()


def _build_topology_layout(group_sizes: list[tuple[str, int]]) -> tuple[dict[str, Any], dict[str, list[tuple[int, int]]]]:
    total_servers = sum(count for _, count in group_sizes)
    if not group_sizes:
        empty_topology = {
            "board_width": 1400,
            "board_height": 900,
            "mainframe_x": 700,
            "mainframe_y": 450,
            "throughput": f"{max(0.8, total_servers * 0.12):.1f} GB/s",
            "group_regions": [],
            "signature": "empty",
            "initial_offset_x": 0,
            "initial_offset_y": -200,
        }
        return empty_topology, {}

    group_layouts: list[dict[str, Any]] = []
    max_local_radius = 180.0
    for group_name, count in group_sizes:
        local_points: list[tuple[float, float]] = []
        if count <= 4:
            columns = 2
        elif count <= 9:
            columns = 3
        elif count <= 16:
            columns = 4
        else:
            columns = 5
        rows = max(1, int(math.ceil(count / columns)))
        gap_x = 136.0
        gap_y = 156.0
        #test

        grid_width = (columns - 1) * gap_x
        grid_height = (rows - 1) * gap_y
        start_x = -(grid_width / 2)
        start_y = -(grid_height / 2)

        for index in range(count):
            row = index // columns
            col = index % columns
            row_items = columns if row < rows - 1 else count - (rows - 1) * columns
            row_shift = ((columns - row_items) * gap_x) / 2 if row_items < columns else 0.0
            x = start_x + row_shift + col * gap_x
            y = start_y + row * gap_y
            local_points.append((x, y))

        point_x = [x for x, _ in local_points] or [0.0]
        point_y = [y for _, y in local_points] or [0.0]
        pad_x = 96.0
        pad_top = 170.0
        pad_bottom = 100.0

        local_left = min(point_x) - pad_x
        local_top = min(point_y) - pad_top
        local_width = max(300.0, (max(point_x) - min(point_x)) + (pad_x * 2))
        local_height = max(300.0, (max(point_y) - min(point_y)) + pad_top + pad_bottom)
        local_radius = max(local_width, local_height) / 2
        max_local_radius = max(max_local_radius, local_radius)

        group_layouts.append(
            {
                "group": group_name,
                "count": count,
                "local_points": local_points,
                "local_left": local_left,
                "local_top": local_top,
                "local_width": local_width,
                "local_height": local_height,
            }
        )

    group_count = len(group_layouts)
    orbit_x = 290.0 + max_local_radius + max(0.0, (group_count - 4) * 22.0)
    orbit_y = 195.0 + max_local_radius * 0.72

    mainframe_x0 = 0.0
    mainframe_y0 = 0.0

    group_regions_pre: list[dict[str, Any]] = []
    group_slots_pre: dict[str, list[tuple[float, float]]] = {}
    for group_index, layout in enumerate(group_layouts):
        if group_count == 1:
            angle = 0.0
        else:
            angle = -(math.pi / 2) + (2 * math.pi * group_index) / group_count

        center_x = mainframe_x0 + orbit_x * math.cos(angle)
        center_y = mainframe_y0 + orbit_y * math.sin(angle)
        points = [(center_x + dx, center_y + dy) for dx, dy in layout["local_points"]]
        group_slots_pre[layout["group"]] = points

        group_regions_pre.append(
            {
                "group": layout["group"],
                "count": layout["count"],
                "left": center_x + float(layout["local_left"]),
                "top": center_y + float(layout["local_top"]),
                "width": float(layout["local_width"]),
                "height": float(layout["local_height"]),
            }
        )

    min_x = mainframe_x0 - 170
    max_x = mainframe_x0 + 170
    min_y = mainframe_y0 - 180
    max_y = mainframe_y0 + 180
    for region in group_regions_pre:
        min_x = min(min_x, float(region["left"]))
        min_y = min(min_y, float(region["top"]))
        max_x = max(max_x, float(region["left"]) + float(region["width"]))
        max_y = max(max_y, float(region["top"]) + float(region["height"]))

    margin = 120.0
    content_width = (max_x - min_x) + (margin * 2)
    content_height = (max_y - min_y) + (margin * 2)
    board_width = max(1400, int(math.ceil(content_width)))
    board_height = max(900, int(math.ceil(content_height)))

    shift_x = margin - min_x + max(0.0, (board_width - content_width) / 2)
    shift_y = margin - min_y + max(0.0, (board_height - content_height) / 2)

    group_regions: list[dict[str, Any]] = []
    for region in group_regions_pre:
        group_regions.append(
            {
                "group": str(region["group"]),
                "count": int(region["count"]),
                "left": int(round(float(region["left"]) + shift_x)),
                "top": int(round(float(region["top"]) + shift_y)),
                "width": int(round(float(region["width"]))),
                "height": int(round(float(region["height"]))),
            }
        )

    group_slots: dict[str, list[tuple[int, int]]] = {}
    for group_name, points in group_slots_pre.items():
        group_slots[group_name] = [(int(round(x + shift_x)), int(round(y + shift_y))) for x, y in points]

    mainframe_x = int(round(mainframe_x0 + shift_x))
    mainframe_y = int(round(mainframe_y0 + shift_y))

    signature_parts = [
        f"{region['group']}:{region['count']}:{region['left']}:{region['top']}:{region['width']}:{region['height']}"
        for region in group_regions
    ]
    topology_signature = (
        f"bw:{board_width}|bh:{board_height}|mx:{mainframe_x}|my:{mainframe_y}|"
        + "|".join(signature_parts)
    )

    topology = {
        "board_width": int(board_width),
        "board_height": int(board_height),
        "mainframe_x": int(mainframe_x),
        "mainframe_y": int(mainframe_y),
        "throughput": f"{max(0.8, total_servers * 0.12):.1f} GB/s",
        "group_regions": group_regions,
        "signature": topology_signature,
        "initial_offset_x": 0,
        "initial_offset_y": -max(0, int(mainframe_y) - 250),
    }
    return topology, group_slots


def _build_live_servers_from_checks() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with SERVER_HEALTH_LOCK:
        checks = [deepcopy(check) for check in server_health_checks]
    if not checks:
        empty_topology, _ = _build_topology_layout([])
        return [], empty_topology

    group_rank = {group: index for index, group in enumerate(SERVER_GROUP_OPTIONS)}
    checks.sort(key=lambda check: (group_rank.get(check.get("server_group", ""), 999), str(check.get("name", "")).lower()))

    now = datetime.now(timezone.utc)
    grouped_checks: dict[str, list[dict[str, Any]]] = {group: [] for group in SERVER_GROUP_OPTIONS}
    for check in checks:
        grouped_checks[_normalize_server_group(check.get("server_group"))].append(check)

    active_groups = [group for group in SERVER_GROUP_OPTIONS if grouped_checks[group]]
    group_sizes = [(group, len(grouped_checks[group])) for group in active_groups]
    topology, group_slots = _build_topology_layout(group_sizes)

    nodes: list[dict[str, Any]] = []
    for group_index, group_name in enumerate(active_groups):
        group_checks = grouped_checks[group_name]
        slots = group_slots.get(group_name) or []

        for local_index, check in enumerate(group_checks):
            if local_index < len(slots):
                x, y = slots[local_index]
            else:
                x = int(topology["mainframe_x"] + 220 + local_index * 20)
                y = int(topology["mainframe_y"] + group_index * 26)

            last_check = check.get("last_check") or {}
            is_up = bool(last_check.get("is_up"))
            checked_at = _parse_checked_at(last_check.get("checked_at"))
            checked_recently = bool(checked_at and (now - checked_at).total_seconds() <= 8)
            enabled = bool(check.get("is_enabled"))

            if not enabled:
                status = "warning"
            elif not last_check:
                status = "warning"
            elif is_up:
                status = "healthy"
            else:
                status = "critical"

            response_time = last_check.get("response_ms")
            if response_time is None:
                response_time = 0
            try:
                response_time_value = int(round(float(response_time)))
            except (TypeError, ValueError):
                response_time_value = 0

            total_checks = int(check.get("total_checks") or 0)
            successful_checks = int(check.get("successful_checks") or 0)
            uptime = round((successful_checks / total_checks) * 100, 1) if total_checks > 0 else 0.0
            ping_color = "#22d3ee" if status == "healthy" else "#facc15" if status == "warning" else "#f87171"
            ping_duration = round(max(0.55, min(2.8, (response_time_value or 500) / 420)), 2)
            ping_delay = round((local_index % 6) * 0.08 + (group_index % 3) * 0.07, 2)

            nodes.append(
                {
                    "id": check["id"],
                    "name": check["name"],
                    "server_group": group_name,
                    "url": check["url"],
                    "is_enabled": enabled,
                    "status": status,
                    "response_time": response_time_value,
                    "uptime": uptime,
                    "x": x,
                    "y": y,
                    "last_check": last_check if last_check else None,
                    "last_ping_at": last_check.get("checked_at"),
                    "last_ping_display": _format_last_ping_display(last_check.get("checked_at")),
                    "http_status": last_check.get("http_status"),
                    "animate_ping": bool(enabled and checked_recently),
                    "ping_color": ping_color,
                    "ping_duration_seconds": ping_duration,
                    "ping_delay_seconds": ping_delay,
                }
            )

    return nodes, topology


def _run_server_health_check(check: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    http_status: int | None = None
    error_message = ""

    auth_header: tuple[str, str] | None = None
    if check["auth_type"] == "basic":
        password = _secret_from_env(check["password_env_key"])
        if not password:
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "http_status": None,
                "expected_status": check["expected_status"],
                "response_ms": 0.0,
                "is_up": False,
                "error": f"Missing password secret in env key {check['password_env_key']}",
            }
        credentials = f"{check['username']}:{password}"
        basic_token = b64encode(credentials.encode("utf-8")).decode("utf-8")
        auth_header = ("Authorization", f"Basic {basic_token}")

    if check["auth_type"] == "bearer":
        bearer_token = _secret_from_env(check["bearer_token_env_key"])
        if not bearer_token:
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "http_status": None,
                "expected_status": check["expected_status"],
                "response_ms": 0.0,
                "is_up": False,
                "error": f"Missing bearer token secret in env key {check['bearer_token_env_key']}",
            }
        auth_header = ("Authorization", f"Bearer {bearer_token}")

    try:
        req = urllib_request.Request(check["url"], method=check["method"])
        req.add_header("User-Agent", "SLA-app-health-check/1.0")
        if auth_header:
            req.add_header(auth_header[0], auth_header[1])

        ssl_context = None
        if not check["verify_tls"]:
            ssl_context = ssl._create_unverified_context()

        with urllib_request.urlopen(
            req,
            timeout=check["timeout_seconds"],
            context=ssl_context,
        ) as response:
            http_status = int(response.getcode() or 0)

    except urllib_error.HTTPError as exc:
        http_status = int(exc.code)
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)

    response_ms = round((perf_counter() - started) * 1000, 2)
    expected_status = check["expected_status"]
    is_up = http_status == expected_status

    if http_status is None and not error_message:
        error_message = "No response received"
    if http_status is not None and not is_up:
        error_message = f"Expected HTTP {expected_status}, got {http_status}"

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "http_status": http_status,
        "expected_status": expected_status,
        "response_ms": response_ms,
        "is_up": is_up,
        "error": error_message,
    }


def _evaluate_and_send_alert(
    check: dict[str, Any],
    previous_last_check: dict[str, Any] | None,
    current_result: dict[str, Any],
) -> dict[str, Any] | None:
    if not check.get("email_alerts_enabled"):
        return None

    recipients = _parse_recipients(str(check.get("alert_recipients") or ""))
    if not recipients:
        return None

    previous_is_up = previous_last_check.get("is_up") if isinstance(previous_last_check, dict) else None
    current_is_up = bool(current_result.get("is_up"))
    last_alert = check.get("last_alert") if isinstance(check.get("last_alert"), dict) else {}
    last_alert_status = str(last_alert.get("status") or "")
    last_alert_at = _parse_checked_at(last_alert.get("sent_at"))
    now_utc = datetime.now(timezone.utc)

    alert_kind = ""
    if not current_is_up:
        should_alert = False
        if previous_is_up in {True, None}:
            should_alert = True
        elif last_alert_status != "down":
            should_alert = True
        elif last_alert_at is None:
            should_alert = True
        else:
            elapsed = (now_utc - last_alert_at).total_seconds()
            should_alert = elapsed >= ALERT_REMINDER_SECONDS

        if should_alert:
            alert_kind = "down"

    elif previous_is_up is False and check.get("alert_on_recovery", True):
        alert_kind = "recovery"

    if not alert_kind:
        return None

    check_name = str(check.get("name") or "Unnamed Check")
    check_url = str(check.get("url") or "")
    http_status = current_result.get("http_status")
    response_ms = current_result.get("response_ms")
    checked_at = current_result.get("checked_at")
    error_message = current_result.get("error") or ""

    if alert_kind == "down":
        subject = f"{EMAIL_SUBJECT_PREFIX} DOWN - {check_name}"
        body = (
            f"Server health check is DOWN.\n\n"
            f"Check: {check_name}\n"
            f"URL: {check_url}\n"
            f"Expected status: {current_result.get('expected_status')}\n"
            f"Observed status: {http_status if http_status is not None else 'N/A'}\n"
            f"Response time: {response_ms} ms\n"
            f"Checked at (UTC): {checked_at}\n"
        )
        if error_message:
            body += f"Error: {error_message}\n"
    else:
        subject = f"{EMAIL_SUBJECT_PREFIX} RECOVERED - {check_name}"
        body = (
            f"Server health check has recovered.\n\n"
            f"Check: {check_name}\n"
            f"URL: {check_url}\n"
            f"Observed status: {http_status if http_status is not None else 'N/A'}\n"
            f"Response time: {response_ms} ms\n"
            f"Checked at (UTC): {checked_at}\n"
        )

    sent, send_error = _send_alert_email(subject, body, recipients)
    return {
        "status": alert_kind,
        "sent_at": now_utc.isoformat(),
        "sent": sent,
        "subject": subject,
        "error": send_error,
    }


def _build_server_health_check_from_form(
    form_data: Any,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = str(form_data.get("name", "")).strip()
    url = str(form_data.get("url", "")).strip()
    server_group = _normalize_server_group(str(form_data.get("server_group", "")))
    if not name or not url:
        raise ValueError("Name and URL are required")

    method = str(form_data.get("method", "GET")).upper()
    if method not in ALLOWED_HTTP_METHODS:
        method = "GET"

    auth_type = str(form_data.get("auth_type", "none")).lower()
    if auth_type not in ALLOWED_AUTH_TYPES:
        auth_type = "none"

    check_id = existing["id"] if existing else uuid4().hex
    username = str(form_data.get("username", "")).strip() if auth_type == "basic" else ""

    password_env_key = existing.get("password_env_key", "") if existing else ""
    bearer_token_env_key = existing.get("bearer_token_env_key", "") if existing else ""

    posted_password = str(form_data.get("password", ""))
    posted_bearer_token = str(form_data.get("bearer_token", ""))
    alert_recipients = str(form_data.get("alert_recipients", "")).strip()
    email_alerts_enabled = form_data.get("email_alerts_enabled") == "on"
    alert_on_recovery = form_data.get("alert_on_recovery") == "on"

    if email_alerts_enabled and not _parse_recipients(alert_recipients):
        raise ValueError("At least one alert recipient is required when email alerts are enabled")

    if auth_type == "basic":
        if not password_env_key:
            password_env_key = _secret_key_for(check_id, "PASSWORD")
        if posted_password:
            _upsert_env_value(password_env_key, posted_password)
        elif not _has_secret(password_env_key):
            raise ValueError("Password required for basic auth")

        if bearer_token_env_key:
            _delete_env_value(bearer_token_env_key)
            bearer_token_env_key = ""

    elif auth_type == "bearer":
        if not bearer_token_env_key:
            bearer_token_env_key = _secret_key_for(check_id, "BEARER_TOKEN")
        if posted_bearer_token:
            _upsert_env_value(bearer_token_env_key, posted_bearer_token)
        elif not _has_secret(bearer_token_env_key):
            raise ValueError("Bearer token required for bearer auth")

        if password_env_key:
            _delete_env_value(password_env_key)
            password_env_key = ""
        username = ""

    else:
        if password_env_key:
            _delete_env_value(password_env_key)
            password_env_key = ""
        if bearer_token_env_key:
            _delete_env_value(bearer_token_env_key)
            bearer_token_env_key = ""
        username = ""

    return {
        "id": check_id,
        "name": name,
        "server_group": server_group,
        "url": url,
        "method": method,
        "auth_type": auth_type,
        "username": username,
        "password_env_key": password_env_key,
        "bearer_token_env_key": bearer_token_env_key,
        "timeout_seconds": _coerce_float(form_data.get("timeout_seconds"), 5.0, 1.0, 30.0),
        "expected_status": _coerce_int(form_data.get("expected_status"), 200, 100, 599),
        "verify_tls": form_data.get("verify_tls") == "on",
        "is_enabled": form_data.get("is_enabled") == "on",
        "email_alerts_enabled": email_alerts_enabled,
        "alert_recipients": alert_recipients,
        "alert_on_recovery": alert_on_recovery,
        "last_alert": existing.get("last_alert") if existing else None,
        "last_check": existing.get("last_check") if existing else None,
        "total_checks": int(existing.get("total_checks") or 0) if existing else 0,
        "successful_checks": int(existing.get("successful_checks") or 0) if existing else 0,
    }


server_health_checks: list[dict[str, Any]] = _load_server_health_checks()


def _background_health_check_loop() -> None:
    while True:
        try:
            _refresh_enabled_server_health_checks(force=True)
        except Exception:  # noqa: BLE001
            # Keep loop alive in production monitoring even if one cycle fails.
            pass
        sleep(HEALTH_CHECK_INTERVAL_SECONDS)


def _start_background_health_checker() -> None:
    global _health_checker_thread

    if app.config.get("TESTING"):
        return

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    with _health_checker_start_lock:
        if _health_checker_thread is not None and _health_checker_thread.is_alive():
            return

        _health_checker_thread = threading.Thread(
            target=_background_health_check_loop,
            name="server-health-checker",
            daemon=True,
        )
        _health_checker_thread.start()


@app.before_request
def ensure_background_checker_started() -> None:
    _start_background_health_checker()


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {"today": date.today().isoformat()}


@app.get("/")
def server_health() -> str:
    _start_background_health_checker()

    with SERVER_HEALTH_LOCK:
        has_configured_servers = bool(server_health_checks)

    servers, topology = _build_live_servers_from_checks()
    grouped_servers: list[dict[str, Any]] = []
    for group in SERVER_GROUP_OPTIONS:
        group_items = [server for server in servers if server.get("server_group") == group]
        if group_items:
            grouped_servers.append({"group": group, "servers": group_items})

    healthy_count = sum(server["status"] == "healthy" for server in servers)
    warning_count = sum(server["status"] == "warning" for server in servers)
    critical_count = sum(server["status"] == "critical" for server in servers)

    return render_template(
        "server_health.html",
        page_title="Server Health",
        active_page="server-health",
        servers=servers,
        topology=topology,
        grouped_servers=grouped_servers,
        has_configured_servers=has_configured_servers,
        stats={
            "total": len(servers),
            "healthy": healthy_count,
            "warning": warning_count,
            "critical": critical_count,
        },
        health_check_interval_seconds=HEALTH_CHECK_INTERVAL_SECONDS,
        health_config_stats=_server_health_stats(),
    )


def _server_health_live_payload() -> dict[str, Any]:
    servers, topology = _build_live_servers_from_checks()
    healthy_count = sum(server["status"] == "healthy" for server in servers)
    warning_count = sum(server["status"] == "warning" for server in servers)
    critical_count = sum(server["status"] == "critical" for server in servers)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "health_check_interval_seconds": HEALTH_CHECK_INTERVAL_SECONDS,
        "stats": {
            "total": len(servers),
            "healthy": healthy_count,
            "warning": warning_count,
            "critical": critical_count,
        },
        "servers": servers,
        "topology": topology,
    }


@app.get("/api/server-health/live")
def server_health_live() -> Any:
    _start_background_health_checker()
    return jsonify(_server_health_live_payload())


@app.post("/server-health/refresh")
def refresh_server_health() -> Any:
    _refresh_enabled_server_health_checks(force=True)
    return redirect(url_for("server_health"))


@app.get("/releases")
def releases() -> str:
    return render_template(
        "releases.html",
        page_title="Releases",
        active_page="releases",
        releases=RELEASES,
        stats={
            "deployed": sum(item["status"] == "deployed" for item in RELEASES),
            "in_progress": sum(item["status"] == "in-progress" for item in RELEASES),
            "scheduled": sum(item["status"] == "scheduled" for item in RELEASES),
        },
    )


@app.get("/sla-payments")
def payments() -> str:
    return render_template(
        "sla_payments.html",
        page_title="SLA Payments",
        active_page="sla-payments",
        payments=sla_payments,
        totals={
            "total": sum(item["amount"] for item in sla_payments),
            "pending": sum(item["amount"] for item in sla_payments if item["status"] == "pending"),
            "processing": sum(item["amount"] for item in sla_payments if item["status"] == "processing"),
            "completed": sum(item["amount"] for item in sla_payments if item["status"] == "completed"),
        },
    )


@app.get("/config")
def config_root():
    return redirect(url_for("server_health_config"))


@app.get("/config/server-health")
def server_health_config() -> str:
    checks_for_view: list[dict[str, Any]] = []
    with SERVER_HEALTH_LOCK:
        checks_snapshot = [dict(check) for check in server_health_checks]

    for check in checks_snapshot:
        checks_for_view.append(
            {
                **check,
                "has_password_secret": _has_secret(check["password_env_key"]),
                "has_bearer_secret": _has_secret(check["bearer_token_env_key"]),
            }
        )

    return render_template(
        "config_server_health.html",
        page_title="Server Health Config",
        active_page="config-server-health",
        checks=checks_for_view,
        server_group_options=SERVER_GROUP_OPTIONS,
        notice_text=_notice_text(
            request.args.get("notice"),
            added=request.args.get("added"),
            skipped=request.args.get("skipped"),
        ),
        health_config_stats=_server_health_stats(),
        health_check_interval_seconds=HEALTH_CHECK_INTERVAL_SECONDS,
        smtp_configured=_smtp_is_configured(),
        env_path=str(ENV_PATH),
    )


@app.post("/config/server-health/add")
def add_server_health_config():
    try:
        new_check = _build_server_health_check_from_form(request.form)
    except ValueError:
        return redirect(url_for("server_health_config", notice="missing-required"))

    with SERVER_HEALTH_LOCK:
        server_health_checks.append(new_check)
    _save_server_health_checks()
    return redirect(url_for("server_health_config", notice="added"))


@app.post("/config/server-health/bulk-add")
def bulk_add_server_health_config():
    bulk_urls = str(request.form.get("bulk_urls", "")).strip()
    if not bulk_urls:
        return redirect(url_for("server_health_config", notice="bulk-empty"))

    fallback_group = _normalize_server_group(str(request.form.get("bulk_server_group", SERVER_GROUP_DEFAULT)))
    method = str(request.form.get("bulk_method", "GET")).upper()
    if method not in ALLOWED_HTTP_METHODS:
        method = "GET"

    timeout_seconds = _coerce_float(request.form.get("bulk_timeout_seconds"), 5.0, 1.0, 30.0)
    expected_status = _coerce_int(request.form.get("bulk_expected_status"), 200, 100, 599)
    verify_tls = request.form.get("bulk_verify_tls") == "on"
    is_enabled = request.form.get("bulk_is_enabled") == "on"
    email_alerts_enabled = request.form.get("bulk_email_alerts_enabled") == "on"
    alert_recipients = str(request.form.get("bulk_alert_recipients", "")).strip()
    alert_on_recovery = request.form.get("bulk_alert_on_recovery") == "on"

    if email_alerts_enabled and not _parse_recipients(alert_recipients):
        return redirect(url_for("server_health_config", notice="bulk-invalid-alerts"))

    lines = bulk_urls.splitlines()
    parsed_rows: list[tuple[str, str, str]] = []
    skipped_count = 0
    for index, line in enumerate(lines, start=1):
        parsed = _parse_bulk_line(line, fallback_group=fallback_group, fallback_index=index)
        if parsed is None:
            if line.strip():
                skipped_count += 1
            continue
        parsed_rows.append(parsed)

    if not parsed_rows and skipped_count == 0:
        return redirect(url_for("server_health_config", notice="bulk-empty"))

    with SERVER_HEALTH_LOCK:
        existing_keys = {
            (str(check.get("url", "")).strip().lower(), _normalize_server_group(check.get("server_group")))
            for check in server_health_checks
        }

        added_count = 0
        for name, url, group in parsed_rows:
            dedupe_key = (url.strip().lower(), group)
            if dedupe_key in existing_keys:
                skipped_count += 1
                continue

            check_id = uuid4().hex
            server_health_checks.append(
                {
                    "id": check_id,
                    "name": name,
                    "server_group": group,
                    "url": url,
                    "method": method,
                    "auth_type": "none",
                    "username": "",
                    "password_env_key": "",
                    "bearer_token_env_key": "",
                    "timeout_seconds": timeout_seconds,
                    "expected_status": expected_status,
                    "verify_tls": verify_tls,
                    "is_enabled": is_enabled,
                    "email_alerts_enabled": email_alerts_enabled,
                    "alert_recipients": alert_recipients,
                    "alert_on_recovery": alert_on_recovery,
                    "last_alert": None,
                    "last_check": None,
                    "total_checks": 0,
                    "successful_checks": 0,
                }
            )
            existing_keys.add(dedupe_key)
            added_count += 1

    _save_server_health_checks()
    return redirect(url_for("server_health_config", notice="bulk-added", added=added_count, skipped=skipped_count))


@app.post("/config/server-health/<check_id>/update")
def update_server_health_config(check_id: str):
    with SERVER_HEALTH_LOCK:
        index, existing = _find_server_health_check(check_id)
        if existing is None:
            return redirect(url_for("server_health_config", notice="missing-target"))

        try:
            updated = _build_server_health_check_from_form(request.form, existing=existing)
        except ValueError:
            return redirect(url_for("server_health_config", notice="missing-required"))

        server_health_checks[index] = updated
    _save_server_health_checks()
    return redirect(url_for("server_health_config", notice="updated"))


@app.post("/config/server-health/<check_id>/delete")
def delete_server_health_config(check_id: str):
    with SERVER_HEALTH_LOCK:
        index, existing = _find_server_health_check(check_id)
        if existing is None:
            return redirect(url_for("server_health_config", notice="missing-target"))

        if existing["password_env_key"]:
            _delete_env_value(existing["password_env_key"])
        if existing["bearer_token_env_key"]:
            _delete_env_value(existing["bearer_token_env_key"])

        del server_health_checks[index]
    _save_server_health_checks()
    return redirect(url_for("server_health_config", notice="deleted"))


@app.post("/config/server-health/<check_id>/test")
def test_server_health_config(check_id: str):
    with SERVER_HEALTH_LOCK:
        _, existing = _find_server_health_check(check_id)
        if existing is None:
            return redirect(url_for("server_health_config", notice="missing-target"))
        check_snapshot = dict(existing)
        previous_last_check = deepcopy(existing.get("last_check")) if isinstance(existing.get("last_check"), dict) else None

    _apply_result = _run_server_health_check(check_snapshot)
    alert_context: dict[str, Any] | None = None
    with SERVER_HEALTH_LOCK:
        _, live_check = _find_server_health_check(check_id)
        if live_check is None:
            return redirect(url_for("server_health_config", notice="missing-target"))
        _apply_check_result(live_check, _apply_result)
        alert_context = deepcopy(live_check)

    if alert_context is not None:
        alert_result = _evaluate_and_send_alert(alert_context, previous_last_check, _apply_result)
        if alert_result is not None:
            with SERVER_HEALTH_LOCK:
                _, live_check = _find_server_health_check(check_id)
                if live_check is not None:
                    live_check["last_alert"] = alert_result
    _save_server_health_checks()
    notice = "tested-up" if _apply_result["is_up"] else "tested-down"
    return redirect(url_for("server_health_config", notice=notice))


@app.post("/config/server-health/test-all")
def test_all_server_health_configs():
    _refresh_enabled_server_health_checks(force=True)
    return redirect(url_for("server_health_config", notice="tested-all"))


@app.post("/api/payments/<payment_id>/reprocess")
def reprocess_payment(payment_id: str):
    for payment in sla_payments:
        if payment["id"] == payment_id and payment["status"] in {"failed", "pending"}:
            payment["status"] = "processing"
            return jsonify({"ok": True, "status": "processing"})
    return jsonify({"ok": False}), 404


if __name__ == "__main__":
    app.run(debug=True)
