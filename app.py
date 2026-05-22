from __future__ import annotations

import ast
import html
import json
import math
import mimetypes
import os
import posixpath
import re
import shlex
import smtplib
import ssl
import stat
import threading
from base64 import b64encode
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib import error as urllib_error
from urllib.parse import urlparse
from urllib import request as urllib_request
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, url_for

from sla_payment_automation import (
    ATTACHMENTS_DIR,
    BACKUP_DIR,
    BASE_DIR,
    BAD_TRANSACTIONS,
    LOG_DIR,
    NEW_JSON_DIR,
    AutomationDependencyError,
    SLAPaymentAutomationRunner,
    parse_app_ids,
    parse_report_date,
)

try:
    import paramiko
except ImportError:
    paramiko = None

app = Flask(__name__)

ALLOWED_HTTP_METHODS = {"GET", "HEAD"}
ALLOWED_AUTH_TYPES = {"none", "basic", "bearer"}
ENV_LINE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
SERVER_HEALTH_CONFIG_PATH = Path(app.instance_path) / "server_health_checks.json"
RELEASE_TRACKER_CONFIG_PATH = Path(app.instance_path) / "release_tracker_config.json"
RELEASE_TRACKER_EVENTS_PATH = Path(app.instance_path) / "release_tracker_events.json"
RELEASE_TRACKER_TARGETS_PATH = Path(app.instance_path) / "release_tracker_targets.json"
ENV_PATH = Path(os.getenv("SLA_APP_ENV_PATH", ".env"))
RELEASE_REF_PATTERN = re.compile(r"\b(?:R\d+(?:\.\d+)+|V\d+(?:\.\d+){1,3}(?:[-+._A-Za-z0-9]*)?)\b", re.IGNORECASE)

try:
    HEALTH_CHECK_INTERVAL_SECONDS = max(2.0, min(300.0, float(os.getenv("SLA_HEALTH_CHECK_INTERVAL_SECONDS", "15"))))
except ValueError:
    HEALTH_CHECK_INTERVAL_SECONDS = 15.0

SERVER_HEALTH_LOCK = threading.RLock()
_health_checker_thread: threading.Thread | None = None
_health_checker_start_lock = threading.Lock()
RELEASE_TRACKER_LOCK = threading.RLock()
_release_tracker_thread: threading.Thread | None = None
_release_tracker_start_lock = threading.Lock()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


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
DEFAULT_HEALTH_ALERT_RECIPIENTS_RAW = (
    os.getenv("SLA_ALERT_DEFAULT_RECIPIENTS", "daniel.desalvatore@its.ny.gov").strip()
    or "daniel.desalvatore@its.ny.gov"
)
RELEASE_NOTIFICATION_SUBJECT_PREFIX = (
    os.getenv("SLA_RELEASE_NOTIFICATION_SUBJECT_PREFIX", "[Release Notification]").strip() or "[Release Notification]"
)
RELEASE_NOTIFICATION_TO_RECIPIENTS = (
    os.getenv("SLA_RELEASE_NOTIFICATION_TO_RECIPIENTS", os.getenv("SLA_RELEASE_NOTIFICATION_RECIPIENTS", "")).strip()
)
RELEASE_NOTIFICATION_CC_RECIPIENTS = os.getenv("SLA_RELEASE_NOTIFICATION_CC_RECIPIENTS", "").strip()
RELEASE_NOTIFICATION_SIGNATURE_HTML = os.getenv("SLA_RELEASE_NOTIFICATION_SIGNATURE_HTML", "")
RELEASE_BACKUP_PASSWORD_ENV_KEY = (
    os.getenv("SLA_RELEASE_BACKUP_PASSWORD_ENV_KEY", "SLA_RELEASE_BACKUP_PASSWORD").strip()
    or "SLA_RELEASE_BACKUP_PASSWORD"
)

RELEASE_TRACKER_DEFAULTS: dict[str, Any] = {
    "is_enabled": False,
    "provider": "file_paths",
    "base_path": os.getenv("SLA_RELEASE_BASE_PATH", "").strip(),
    "poll_interval_seconds": _env_int("SLA_RELEASE_POLL_INTERVAL_SECONDS", 180, 30, 86_400),
    "notification_to_recipients": RELEASE_NOTIFICATION_TO_RECIPIENTS,
    "notification_cc_recipients": RELEASE_NOTIFICATION_CC_RECIPIENTS,
    "notification_signature_html": RELEASE_NOTIFICATION_SIGNATURE_HTML,
    "backup_targets": [],
    "backup_host": os.getenv("SLA_RELEASE_BACKUP_HOST", "").strip(),
    "backup_port": _env_int("SLA_RELEASE_BACKUP_PORT", 22, 1, 65_535),
    "backup_username": os.getenv("SLA_RELEASE_BACKUP_USERNAME", "").strip(),
    "backup_source_path": os.getenv("SLA_RELEASE_BACKUP_SOURCE_PATH", "").strip(),
    "backup_destination_path": os.getenv("SLA_RELEASE_BACKUP_DESTINATION_PATH", "").strip(),
    "backup_last_run_at": "",
    "backup_last_status": "",
    "backup_last_message": "",
    "backup_last_destination": "",
    "last_run_at": "",
    "last_error": "",
}
RELEASE_FOLDER_MAX_AGE = timedelta(days=365)
RELEASE_STEP_OPTIONS = ("", "DEV", "QA", "STAGE", "PROD")
RELEASE_BACKUP_ENV_OPTIONS = ("QA", "STAGE")

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


def _secret_key_for_release_backup_target(target_id: str) -> str:
    sanitized_id = re.sub(r"[^A-Za-z0-9]", "_", str(target_id or "")).upper() or "TARGET"
    return f"SLA_RELEASE_BACKUP_{sanitized_id}_PASSWORD"


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


def _health_alert_recipients(raw: str) -> list[str]:
    recipients = _parse_recipients(raw)
    default_recipients = _parse_recipients(DEFAULT_HEALTH_ALERT_RECIPIENTS_RAW)
    seen: set[str] = set()
    merged: list[str] = []
    for recipient in [*recipients, *default_recipients]:
        normalized = recipient.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(recipient)
    return merged


def _smtp_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def _send_alert_email(
    subject: str,
    body: str,
    to_recipients: list[str],
    *,
    cc_recipients: list[str] | None = None,
    html_body: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    cc_recipients = [recipient for recipient in (cc_recipients or []) if recipient]
    all_recipients = [recipient for recipient in [*to_recipients, *cc_recipients] if recipient]
    if not all_recipients:
        return False, "No recipients configured"
    if not _smtp_is_configured():
        return False, "SMTP is not configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    if to_recipients:
        message["To"] = ", ".join(to_recipients)
    if cc_recipients:
        message["Cc"] = ", ".join(cc_recipients)
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for attachment in attachments or []:
        filename = str(attachment.get("filename") or "").strip()
        content = attachment.get("content")
        if not filename or not isinstance(content, (bytes, bytearray)):
            continue
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type and "/" in guessed_type:
            maintype, subtype = guessed_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(bytes(content), maintype=maintype, subtype=subtype, filename=filename)

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message, to_addrs=all_recipients)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.ehlo()
                if SMTP_USE_TLS:
                    smtp.starttls()
                    smtp.ehlo()
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message, to_addrs=all_recipients)
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
        "email_alerts_enabled": bool(raw.get("email_alerts_enabled", True)),
        "email_alerts_initialized": bool(raw.get("email_alerts_initialized", False)),
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

        # One-time default migration: enable email alerts for existing checks,
        # while allowing manual overrides after initialization.
        if not normalized.get("email_alerts_initialized"):
            normalized["email_alerts_enabled"] = True
            normalized["email_alerts_initialized"] = True
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


def _normalize_release_backup_environment(value: Any, fallback: str = "QA") -> str:
    normalized = str(value or "").strip().upper()
    if normalized in RELEASE_BACKUP_ENV_OPTIONS:
        return normalized
    inferred = _infer_deployment_step(normalized)
    if inferred in RELEASE_BACKUP_ENV_OPTIONS:
        return inferred
    return fallback


def _normalize_release_backup_target(raw: dict[str, Any], *, fallback_index: int = 1) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    target_id = str(raw.get("id") or uuid4().hex).strip() or uuid4().hex
    host = str(raw.get("host") or raw.get("backup_host") or "").strip()
    username = str(raw.get("username") or raw.get("backup_username") or "").strip()
    source_path = str(raw.get("source_path") or raw.get("backup_source_path") or "").strip()
    destination_path = str(raw.get("destination_path") or raw.get("backup_destination_path") or "").strip()
    label = str(raw.get("label") or "").strip()
    environment_value = raw.get("environment")
    if not environment_value:
        environment_value = _infer_deployment_step(f"{host} {source_path} {destination_path}")
    environment = _normalize_release_backup_environment(environment_value, "QA")

    if not any([host, username, source_path, destination_path, label, environment_value]):
        return None

    source_name = posixpath.basename(str(source_path).rstrip("/")) or f"target-{fallback_index}"
    return {
        "id": target_id,
        "label": label or f"{environment} {source_name}",
        "environment": environment,
        "host": host,
        "port": _coerce_int(raw.get("port") or raw.get("backup_port"), 22, 1, 65_535),
        "username": username,
        "source_path": source_path,
        "destination_path": destination_path,
        "password_env_key": str(raw.get("password_env_key") or _secret_key_for_release_backup_target(target_id)).strip()
        or _secret_key_for_release_backup_target(target_id),
        "is_enabled": bool(raw.get("is_enabled", True)),
        "last_run_at": str(raw.get("last_run_at") or "").strip(),
        "last_status": str(raw.get("last_status") or "").strip(),
        "last_message": str(raw.get("last_message") or "").strip(),
        "last_destination": str(raw.get("last_destination") or "").strip(),
    }


def _legacy_release_backup_target(raw: dict[str, Any]) -> dict[str, Any] | None:
    legacy_host = str(raw.get("backup_host") or "").strip()
    legacy_username = str(raw.get("backup_username") or "").strip()
    legacy_source = str(raw.get("backup_source_path") or "").strip()
    legacy_destination = str(raw.get("backup_destination_path") or "").strip()
    if not any([legacy_host, legacy_username, legacy_source, legacy_destination]):
        return None

    return _normalize_release_backup_target(
        {
            "id": "legacy",
            "label": str(raw.get("backup_label") or "").strip(),
            "environment": _infer_deployment_step(f"{legacy_host} {legacy_source} {legacy_destination}"),
            "host": legacy_host,
            "port": raw.get("backup_port"),
            "username": legacy_username,
            "source_path": legacy_source,
            "destination_path": legacy_destination,
            "password_env_key": RELEASE_BACKUP_PASSWORD_ENV_KEY,
            "is_enabled": True,
            "last_run_at": raw.get("backup_last_run_at"),
            "last_status": raw.get("backup_last_status"),
            "last_message": raw.get("backup_last_message"),
            "last_destination": raw.get("backup_last_destination"),
        },
        fallback_index=1,
    )


def _normalize_release_backup_targets(raw_targets: Any, legacy_source: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []

    if isinstance(raw_targets, list):
        for index, item in enumerate(raw_targets, start=1):
            normalized = _normalize_release_backup_target(item, fallback_index=index)
            if normalized is not None:
                targets.append(normalized)
        return targets

    legacy_target = _legacy_release_backup_target(legacy_source)
    if legacy_target is None:
        return []
    return [legacy_target]


def _normalize_release_tracker_config(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_enabled": bool(raw.get("is_enabled", RELEASE_TRACKER_DEFAULTS["is_enabled"])),
        "provider": "file_paths",
        "base_path": str(raw.get("base_path") or RELEASE_TRACKER_DEFAULTS["base_path"]).strip(),
        "poll_interval_seconds": _coerce_int(
            raw.get("poll_interval_seconds"),
            int(RELEASE_TRACKER_DEFAULTS["poll_interval_seconds"]),
            30,
            86_400,
        ),
        "notification_to_recipients": str(
            raw.get("notification_to_recipients") or RELEASE_TRACKER_DEFAULTS["notification_to_recipients"]
        ).strip(),
        "notification_cc_recipients": str(
            raw.get("notification_cc_recipients") or RELEASE_TRACKER_DEFAULTS["notification_cc_recipients"]
        ).strip(),
        "notification_signature_html": str(
            raw.get("notification_signature_html") or RELEASE_TRACKER_DEFAULTS["notification_signature_html"]
        ),
        "backup_targets": _normalize_release_backup_targets(raw.get("backup_targets"), raw),
        "backup_host": str(raw.get("backup_host") or RELEASE_TRACKER_DEFAULTS["backup_host"]).strip(),
        "backup_port": _coerce_int(raw.get("backup_port"), int(RELEASE_TRACKER_DEFAULTS["backup_port"]), 1, 65_535),
        "backup_username": str(raw.get("backup_username") or RELEASE_TRACKER_DEFAULTS["backup_username"]).strip(),
        "backup_source_path": str(raw.get("backup_source_path") or RELEASE_TRACKER_DEFAULTS["backup_source_path"]).strip(),
        "backup_destination_path": str(
            raw.get("backup_destination_path") or RELEASE_TRACKER_DEFAULTS["backup_destination_path"]
        ).strip(),
        "backup_last_run_at": str(raw.get("backup_last_run_at") or "").strip(),
        "backup_last_status": str(raw.get("backup_last_status") or "").strip(),
        "backup_last_message": str(raw.get("backup_last_message") or "").strip(),
        "backup_last_destination": str(raw.get("backup_last_destination") or "").strip(),
        "last_run_at": str(raw.get("last_run_at") or "").strip(),
        "last_error": str(raw.get("last_error") or "").strip(),
    }


def _load_release_tracker_config() -> dict[str, Any]:
    _ensure_instance_dir()
    if not RELEASE_TRACKER_CONFIG_PATH.exists():
        return _normalize_release_tracker_config(RELEASE_TRACKER_DEFAULTS)

    try:
        payload = json.loads(RELEASE_TRACKER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _normalize_release_tracker_config(RELEASE_TRACKER_DEFAULTS)

    if not isinstance(payload, dict):
        return _normalize_release_tracker_config(RELEASE_TRACKER_DEFAULTS)

    return _normalize_release_tracker_config(payload)


def _save_release_tracker_config() -> None:
    _ensure_instance_dir()
    with RELEASE_TRACKER_LOCK:
        payload = json.dumps(release_tracker_config, indent=2)
    RELEASE_TRACKER_CONFIG_PATH.write_text(payload, encoding="utf-8")


def _save_release_notification_defaults(to_recipients_raw: str, cc_recipients_raw: str) -> None:
    with RELEASE_TRACKER_LOCK:
        release_tracker_config["notification_to_recipients"] = to_recipients_raw.strip()
        release_tracker_config["notification_cc_recipients"] = cc_recipients_raw.strip()
    _save_release_tracker_config()


def _release_backup_target_is_ready(target: dict[str, Any]) -> bool:
    return bool(
        paramiko is not None
        and bool(target.get("is_enabled", True))
        and str(target.get("host") or "").strip()
        and str(target.get("username") or "").strip()
        and str(target.get("source_path") or "").strip()
        and str(target.get("destination_path") or "").strip()
        and _has_secret(str(target.get("password_env_key") or ""))
    )


def _build_release_backup_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = dict(config or release_tracker_config)
    raw_targets = snapshot.get("backup_targets")
    targets_for_view: list[dict[str, Any]] = []
    qa_targets = 0
    stage_targets = 0
    ready_targets = 0
    enabled_targets = 0

    if isinstance(raw_targets, list):
        for target in raw_targets:
            if not isinstance(target, dict):
                continue
            target_view = dict(target)
            environment = _normalize_release_backup_environment(target_view.get("environment"), "QA")
            target_view["environment"] = environment
            target_view["has_password"] = _has_secret(str(target_view.get("password_env_key") or ""))
            target_view["is_ready"] = _release_backup_target_is_ready(target_view)
            targets_for_view.append(target_view)
            if environment == "QA":
                qa_targets += 1
            elif environment == "STAGE":
                stage_targets += 1
            if bool(target_view.get("is_enabled", True)):
                enabled_targets += 1
                if target_view["is_ready"]:
                    ready_targets += 1

    return {
        "targets": targets_for_view,
        "configured_targets": len(targets_for_view),
        "enabled_targets": enabled_targets,
        "ready_targets": ready_targets,
        "qa_targets": qa_targets,
        "stage_targets": stage_targets,
        "is_ready": ready_targets > 0,
        "paramiko_available": paramiko is not None,
        "last_run_at": str(snapshot.get("backup_last_run_at") or "").strip(),
        "last_status": str(snapshot.get("backup_last_status") or "").strip(),
        "last_message": str(snapshot.get("backup_last_message") or "").strip(),
        "last_destination": str(snapshot.get("backup_last_destination") or "").strip(),
    }


def _release_backup_is_ready(config: dict[str, Any] | None = None) -> bool:
    return bool(_build_release_backup_state(config).get("is_ready"))


def _sanitize_backup_label(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    return normalized.strip("-._")[:80]


def _normalize_remote_directory(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "/":
        return raw
    return raw.rstrip("/")


def _remote_backup_folder_name(source_path: str, backup_label: str) -> str:
    source_name = posixpath.basename(str(source_path or "").rstrip("/")) or "release"
    safe_source = _sanitize_backup_label(source_name) or "release"
    safe_label = _sanitize_backup_label(backup_label)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if safe_label:
        return f"{safe_source}_{safe_label}_{timestamp}"
    return f"{safe_source}_{timestamp}"


def _record_release_backup_batch_result(*, results: list[dict[str, Any]], summary_message: str, is_ok: bool) -> None:
    recorded_at = datetime.now(timezone.utc).isoformat()
    results_by_id = {str(item.get("id") or "").strip(): item for item in results if str(item.get("id") or "").strip()}
    summary_destinations = [str(item.get("destination") or "").strip() for item in results if str(item.get("destination") or "").strip()]

    with RELEASE_TRACKER_LOCK:
        updated_targets: list[dict[str, Any]] = []
        for target in release_tracker_config.get("backup_targets") or []:
            if not isinstance(target, dict):
                continue
            updated_target = dict(target)
            result = results_by_id.get(str(updated_target.get("id") or "").strip())
            if result is not None:
                updated_target["last_run_at"] = recorded_at
                updated_target["last_status"] = "success" if bool(result.get("ok")) else "error"
                updated_target["last_message"] = str(result.get("message") or "").strip()
                updated_target["last_destination"] = str(result.get("destination") or "").strip()
            updated_targets.append(_normalize_release_backup_target(updated_target, fallback_index=len(updated_targets) + 1) or updated_target)
        release_tracker_config["backup_targets"] = updated_targets
        release_tracker_config["backup_last_run_at"] = recorded_at
        release_tracker_config["backup_last_status"] = "success" if is_ok else "error"
        release_tracker_config["backup_last_message"] = str(summary_message or "").strip()
        release_tracker_config["backup_last_destination"] = "; ".join(summary_destinations[:5])
    _save_release_tracker_config()


def _run_release_backup_target(target: dict[str, Any], *, backup_label: str = "") -> tuple[bool, str, str]:
    if paramiko is None:
        return False, "", "SSH backup requires the Paramiko package. Install requirements first."

    host = str(target.get("host") or "").strip()
    port = _coerce_int(target.get("port"), 22, 1, 65_535)
    username = str(target.get("username") or "").strip()
    source_path = _normalize_remote_directory(str(target.get("source_path") or ""))
    destination_root = _normalize_remote_directory(str(target.get("destination_path") or ""))
    password = _secret_from_env(str(target.get("password_env_key") or ""))

    if not host or not username or not password or not source_path or not destination_root:
        return (
            False,
            "",
            "Backup target is not fully configured. Save host, username, password, source path, and destination path first.",
        )

    if destination_root == source_path or destination_root.startswith(f"{source_path}/"):
        return False, "", "Backup destination cannot be the same as the source folder or live inside it."

    destination_path = posixpath.join(destination_root, _remote_backup_folder_name(source_path, backup_label))
    quoted_source = shlex.quote(source_path)
    quoted_destination_root = shlex.quote(destination_root)
    quoted_destination = shlex.quote(destination_path)
    remote_command = (
        f"mkdir -p {quoted_destination_root} && "
        f"test -d {quoted_source} && "
        f"test ! -e {quoted_destination} && "
        f"mkdir -p {quoted_destination} && "
        f"cp -a {quoted_source}/. {quoted_destination}/"
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        sftp = client.open_sftp()
        try:
            source_attrs = sftp.stat(source_path)
        except OSError:
            return False, "", f"Remote source path was not found: {source_path}"
        if not stat.S_ISDIR(source_attrs.st_mode):
            return False, "", f"Remote source path is not a folder: {source_path}"
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=180)
        stdin.close()
        exit_status = stdout.channel.recv_exit_status()
        stderr_output = stderr.read().decode("utf-8", errors="replace").strip()
        if exit_status != 0:
            return False, "", stderr_output or "Remote backup command failed."
        try:
            destination_attrs = sftp.stat(destination_path)
        except OSError:
            return False, "", "Backup command completed but the destination folder could not be verified."
        if not stat.S_ISDIR(destination_attrs.st_mode):
            return False, "", "Backup destination exists but is not a folder."
        return True, destination_path, f"Backup created at {destination_path}"
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)
    finally:
        try:
            sftp.close()
        except Exception:  # noqa: BLE001
            pass
        client.close()


def _test_release_backup_target_connection(target: dict[str, Any], *, password_override: str | None = None) -> tuple[bool, str]:
    if paramiko is None:
        return False, "SSH backup requires the Paramiko package. Install requirements first."

    host = str(target.get("host") or "").strip()
    port = _coerce_int(target.get("port"), 22, 1, 65_535)
    username = str(target.get("username") or "").strip()
    source_path = _normalize_remote_directory(str(target.get("source_path") or ""))
    destination_root = _normalize_remote_directory(str(target.get("destination_path") or ""))
    password = password_override if password_override is not None else _secret_from_env(str(target.get("password_env_key") or ""))

    if not host or not username or not password:
        return False, "Host, username, and password are required for the SSH backup test."

    if source_path and destination_root and (destination_root == source_path or destination_root.startswith(f"{source_path}/")):
        return False, "Backup destination cannot be the same as the source folder or live inside it."

    quoted_destination_root = shlex.quote(destination_root)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None

    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        sftp = client.open_sftp()
        label = str(target.get("label") or target.get("host") or "target").strip()
        if not source_path or not destination_root:
            return True, f"SSH/SFTP connection passed for {label} on {host}:{port}. Folder paths were not checked."
        try:
            source_attrs = sftp.stat(source_path)
        except OSError:
            return False, f"Connected, but the remote source path was not found: {source_path}"
        if not stat.S_ISDIR(source_attrs.st_mode):
            return False, f"Connected, but the remote source path is not a folder: {source_path}"

        stdin, stdout, stderr = client.exec_command(f"mkdir -p {quoted_destination_root}", timeout=60)
        stdin.close()
        exit_status = stdout.channel.recv_exit_status()
        stderr_output = stderr.read().decode("utf-8", errors="replace").strip()
        if exit_status != 0:
            return False, stderr_output or f"Connected, but could not prepare the destination path: {destination_root}"

        try:
            destination_attrs = sftp.stat(destination_root)
        except OSError:
            return False, f"Connected, but the destination path could not be verified: {destination_root}"
        if not stat.S_ISDIR(destination_attrs.st_mode):
            return False, f"Connected, but the destination path is not a folder: {destination_root}"

        return True, f"SSH backup connection passed for {label} on {host}:{port}."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            if sftp is not None:
                sftp.close()
        except Exception:  # noqa: BLE001
            pass
        client.close()


def _run_release_backups(*, environment: str, backup_label: str = "") -> tuple[bool, str, list[dict[str, Any]]]:
    with RELEASE_TRACKER_LOCK:
        configured_targets = [deepcopy(item) for item in release_tracker_config.get("backup_targets") or [] if isinstance(item, dict)]

    selected_environment = str(environment or "all").strip().lower()
    eligible_targets = [target for target in configured_targets if bool(target.get("is_enabled", True))]
    if selected_environment in {"qa", "stage"}:
        target_environment = selected_environment.upper()
        eligible_targets = [target for target in eligible_targets if str(target.get("environment") or "").strip().upper() == target_environment]
    else:
        target_environment = "ALL"

    if not eligible_targets:
        if target_environment == "ALL":
            return False, "No enabled backup targets are configured.", []
        return False, f"No enabled {target_environment} backup targets are configured.", []

    results: list[dict[str, Any]] = []
    success_count = 0
    for target in eligible_targets:
        ok, destination, message = _run_release_backup_target(target, backup_label=backup_label)
        results.append(
            {
                "id": str(target.get("id") or "").strip(),
                "label": str(target.get("label") or "").strip() or str(target.get("host") or "").strip() or "Backup target",
                "environment": str(target.get("environment") or "").strip().upper(),
                "ok": ok,
                "destination": destination,
                "message": message,
            }
        )
        if ok:
            success_count += 1

    failure_count = len(results) - success_count
    scope_label = "ALL" if target_environment == "ALL" else target_environment
    if len(results) == 1:
        single = results[0]
        prefix = f"{scope_label} backup" if target_environment != "ALL" else "Backup"
        summary = f"{prefix} for {single['label']}: {single['message']}"
        return bool(single["ok"]), summary, results

    summary = f"{scope_label} backups finished. {success_count} succeeded, {failure_count} failed."
    if failure_count:
        failed_labels = ", ".join(item["label"] for item in results if not item["ok"])
        if failed_labels:
            summary = f"{summary} Failed: {failed_labels}."
    return failure_count == 0, summary, results


def _normalize_release_tracker_target(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    file_path = str(raw.get("file_path") or "").strip()
    if not file_path:
        return None

    deployment_step_override = str(raw.get("deployment_step_override") or "").strip().upper()
    if deployment_step_override not in RELEASE_STEP_OPTIONS:
        deployment_step_override = ""

    return {
        "id": str(raw.get("id") or uuid4().hex),
        "file_path": file_path,
        "folder_name": str(raw.get("folder_name") or Path(file_path).name or file_path).strip(),
        "label": str(raw.get("label") or "").strip(),
        "release_key_override": str(raw.get("release_key_override") or "").strip(),
        "deployment_step_override": deployment_step_override,
        "is_enabled": bool(raw.get("is_enabled", True)),
        "exists": bool(raw.get("exists", False)),
        "last_seen_at": str(raw.get("last_seen_at") or "").strip(),
        "last_modified_at": str(raw.get("last_modified_at") or "").strip(),
        "file_size": _coerce_int(raw.get("file_size"), 0, 0, 10_000_000_000),
    }


def _load_release_tracker_targets() -> list[dict[str, Any]]:
    _ensure_instance_dir()
    if not RELEASE_TRACKER_TARGETS_PATH.exists():
        return []
    try:
        payload = json.loads(RELEASE_TRACKER_TARGETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, list):
        return []

    targets: list[dict[str, Any]] = []
    for item in payload:
        normalized = _normalize_release_tracker_target(item)
        if normalized is not None:
            targets.append(normalized)
    return targets


def _save_release_tracker_targets() -> None:
    _ensure_instance_dir()
    with RELEASE_TRACKER_LOCK:
        payload = json.dumps(release_tracker_targets, indent=2)
    RELEASE_TRACKER_TARGETS_PATH.write_text(payload, encoding="utf-8")


def _normalize_release_tracker_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source_uids_raw = raw.get("source_uids")
    source_uids: list[str] = []
    if isinstance(source_uids_raw, list):
        source_uids = [str(value).strip() for value in source_uids_raw if str(value).strip()]
    source_uid = str(raw.get("source_uid") or "").strip()
    if source_uid and source_uid not in source_uids:
        source_uids.append(source_uid)
    source_uids = source_uids[-50:]

    release_key = str(raw.get("release_key") or "").strip()
    if not release_key:
        version_value = str(raw.get("version") or "").strip()
        if version_value and version_value.lower() != "n/a":
            release_key = version_value

    return {
        "id": str(raw.get("id") or uuid4().hex),
        "source_type": str(raw.get("source_type") or "").strip(),
        "version": str(raw.get("version") or "n/a").strip() or "n/a",
        "release_key": release_key,
        "name": str(raw.get("name") or "Imported Deployment").strip() or "Imported Deployment",
        "status": str(raw.get("status") or "deployed").strip() or "deployed",
        "environment": str(raw.get("environment") or "production").strip() or "production",
        "deployment_step": str(raw.get("deployment_step") or "").strip().upper(),
        "deployed_by": str(raw.get("deployed_by") or "Email Ingest").strip() or "Email Ingest",
        "deployed_at": str(raw.get("deployed_at") or datetime.now().strftime("%Y-%m-%d %H:%M")).strip(),
        "services": _coerce_int(raw.get("services"), 1, 0, 10_000),
        "commits": _coerce_int(raw.get("commits"), 0, 0, 100_000),
        "deployment_file_path": str(raw.get("deployment_file_path") or "").strip(),
        "tracked_paths_count": _coerce_int(raw.get("tracked_paths_count"), 0, 0, 10_000),
        "available_paths_count": _coerce_int(raw.get("available_paths_count"), 0, 0, 10_000),
        "file_exists": bool(raw.get("file_exists", False)),
        "last_modified_at": str(raw.get("last_modified_at") or "").strip(),
        "tracked_file_paths": [
            str(value).strip()
            for value in (raw.get("tracked_file_paths") or [])
            if str(value).strip()
        ],
        "source_uid": str(raw.get("source_uid") or "").strip(),
        "source_uids": source_uids,
        "source_thread_id": str(raw.get("source_thread_id") or "").strip(),
        "source_subject": str(raw.get("source_subject") or "").strip(),
        "source_doc_ref": str(raw.get("source_doc_ref") or "").strip(),
        "imported_at": str(raw.get("imported_at") or datetime.now(timezone.utc).isoformat()).strip(),
    }


def _load_release_tracker_events() -> list[dict[str, Any]]:
    _ensure_instance_dir()
    if not RELEASE_TRACKER_EVENTS_PATH.exists():
        return []
    try:
        payload = json.loads(RELEASE_TRACKER_EVENTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, list):
        return []

    events: list[dict[str, Any]] = []
    for item in payload:
        normalized = _normalize_release_tracker_event(item)
        if normalized is not None:
            events.append(normalized)
    return events


def _save_release_tracker_events() -> None:
    _ensure_instance_dir()
    with RELEASE_TRACKER_LOCK:
        payload = json.dumps(release_tracker_events, indent=2)
    RELEASE_TRACKER_EVENTS_PATH.write_text(payload, encoding="utf-8")


def _canonical_release_key(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return re.sub(r"\s+", "", raw)


def _release_target_path_key(value: str | None) -> str:
    return str(Path(str(value or "").strip()).expanduser()).strip().lower()


def _extract_release_reference(text: str) -> str:
    match = RELEASE_REF_PATTERN.search(text or "")
    if not match:
        return ""
    token = match.group(0).strip()
    if not token:
        return ""
    if token[0] in {"r", "R"}:
        return f"R{token[1:]}"
    if token[0] in {"v", "V"}:
        return f"v{token[1:]}"
    return token


def _infer_deployment_step(text: str) -> str:
    lowered = str(text or "").lower()
    if re.search(r"\bqa\b", lowered):
        return "QA"
    if re.search(r"\bstage(?:d|s|ing)?\b|\bstg\b", lowered):
        return "STAGE"
    if re.search(r"\bprod(?:uction)?\b", lowered):
        return "PROD"
    if re.search(r"\bdev(?:elopment)?\b", lowered):
        return "DEV"
    return ""


def _environment_for_step(step: str, fallback_text: str) -> str:
    if step == "QA":
        return "development"
    if step == "STAGE":
        return "staging"
    if step == "PROD":
        return "production"
    if step == "DEV":
        return "development"
    return _infer_release_environment(fallback_text)


def _infer_release_status(text: str) -> str:
    lowered = str(text or "").lower()
    if re.search(r"\b(fail(?:ed|ure)?|rollback|rolled back|aborted|error)\b", lowered):
        return "failed"
    if re.search(r"\b(scheduled|queued|pending|awaiting approval)\b", lowered):
        return "scheduled"
    if re.search(r"\b(in[ -]?progress|deploying|starting|rollout|rolling out|promoting)\b", lowered):
        return "in-progress"
    if re.search(r"\b(has deployed|deployed|deploy complete|deployment complete|is live|succeeded|success)\b", lowered):
        return "deployed"
    return "deployed"


def _infer_release_environment(deployment_path: str) -> str:
    lowered = deployment_path.lower()
    if "stage" in lowered:
        return "staging"
    if "qa" in lowered:
        return "development"
    if "prod" in lowered:
        return "production"
    return "production"


def _release_step_rank(step: str) -> int:
    return {"": 0, "DEV": 1, "QA": 2, "STAGE": 3, "PROD": 4}.get(str(step or "").upper(), 0)


def _normalize_file_release_status(*, file_exists: bool, deployment_step: str) -> str:
    if not file_exists:
        return "scheduled"
    if str(deployment_step).upper() == "PROD":
        return "deployed"
    if str(deployment_step).upper() in {"DEV", "QA", "STAGE"}:
        return "in-progress"
    return "deployed"


def _build_release_events_from_base_path(
    base_path_value: str,
    existing_events: list[dict[str, Any]],
    existing_targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    base_path_raw = str(base_path_value or "").strip()
    if not base_path_raw:
        return [], [], 0

    base_path = Path(base_path_raw).expanduser()
    if not base_path.exists():
        raise FileNotFoundError(f"Release base path was not found: {base_path}")
    if not base_path.is_dir():
        raise NotADirectoryError(f"Release base path is not a directory: {base_path}")

    now_utc = datetime.now(timezone.utc)
    updated_targets: list[dict[str, Any]] = []
    existing_ids_by_key = {
        _canonical_release_key(str(event.get("release_key") or str(event.get("version") or ""))): str(event.get("id") or "")
        for event in existing_events
    }
    existing_targets_by_path = {
        _release_target_path_key(str(target.get("file_path") or "")): deepcopy(target)
        for target in existing_targets
        if str(target.get("file_path") or "").strip()
    }
    grouped: dict[str, dict[str, Any]] = {}
    try:
        release_dirs = sorted((item for item in base_path.iterdir() if item.is_dir()), key=lambda item: item.name.lower())
    except OSError as exc:
        raise RuntimeError(f"Could not read release base path {base_path}: {exc}") from exc

    eligible_count = 0
    for release_dir in release_dirs:
        path_value = str(release_dir)
        path_key = _release_target_path_key(path_value)
        directory_name = release_dir.name.strip() or path_value
        saved_target = existing_targets_by_path.get(path_key) or {}

        try:
            stats = release_dir.stat()
        except OSError:
            continue

        modified_at = datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc)
        if modified_at < now_utc - RELEASE_FOLDER_MAX_AGE:
            continue

        eligible_count += 1
        last_modified_at = modified_at.isoformat()
        release_ref_default = _extract_release_reference(directory_name) or directory_name
        release_ref = str(saved_target.get("release_key_override") or "").strip() or release_ref_default
        release_key = _canonical_release_key(release_ref or directory_name)
        deployment_step = str(saved_target.get("deployment_step_override") or "").strip().upper()
        if deployment_step not in RELEASE_STEP_OPTIONS or not deployment_step:
            deployment_step = _infer_deployment_step(path_value)
        environment = _environment_for_step(deployment_step, path_value)
        status = _normalize_file_release_status(file_exists=True, deployment_step=deployment_step)
        custom_label = str(saved_target.get("label") or "").strip()
        display_name = custom_label or directory_name
        modified_dt = _parse_checked_at(last_modified_at)
        is_enabled = bool(saved_target.get("is_enabled", True))

        updated_targets.append(
            _normalize_release_tracker_target(
                {
                    "id": str(saved_target.get("id") or uuid4().hex),
                    "file_path": path_value,
                    "folder_name": directory_name,
                    "label": custom_label,
                    "release_key_override": str(saved_target.get("release_key_override") or "").strip(),
                    "deployment_step_override": deployment_step,
                    "is_enabled": is_enabled,
                    "exists": True,
                    "last_seen_at": now_utc.isoformat(),
                    "last_modified_at": last_modified_at,
                    "file_size": int(stats.st_size),
                }
            )
            or {
                "id": str(saved_target.get("id") or uuid4().hex),
                "file_path": path_value,
                "folder_name": directory_name,
                "label": custom_label,
                "release_key_override": str(saved_target.get("release_key_override") or "").strip(),
                "deployment_step_override": deployment_step,
                "is_enabled": is_enabled,
                "exists": True,
                "last_seen_at": now_utc.isoformat(),
                "last_modified_at": last_modified_at,
                "file_size": int(stats.st_size),
            }
        )
        if not is_enabled:
            continue

        event = grouped.get(release_key)
        if event is None:
            existing_id = existing_ids_by_key.get(release_key)
            event = {
                "id": existing_id or uuid4().hex,
                "source_type": "release_folder",
                "version": release_ref or directory_name,
                "release_key": release_ref or directory_name,
                "name": display_name,
                "status": status,
                "environment": environment,
                "deployment_step": deployment_step,
                "deployed_by": "Folder Tracker",
                "deployed_at": modified_dt.astimezone().strftime("%Y-%m-%d %H:%M") if modified_dt is not None else "Not found",
                "services": 1,
                "commits": 0,
                "deployment_file_path": path_value,
                "tracked_paths_count": 1,
                "available_paths_count": 1,
                "file_exists": True,
                "last_modified_at": last_modified_at,
                "tracked_file_paths": [path_value],
                "source_uid": "",
                "source_uids": [],
                "source_thread_id": "",
                "source_subject": "",
                "source_doc_ref": "",
                "imported_at": now_utc.isoformat(),
            }
            grouped[release_key] = event

    normalized_events: list[dict[str, Any]] = []
    for grouped_event in grouped.values():
        normalized = _normalize_release_tracker_event(grouped_event)
        if normalized is not None:
            normalized_events.append(normalized)

    normalized_events.sort(key=_release_sort_key, reverse=True)
    return updated_targets, normalized_events, eligible_count


def _release_sort_key(item: dict[str, Any]) -> datetime:
    imported_at = str(item.get("imported_at") or "").strip()
    if imported_at:
        parsed = _parse_checked_at(imported_at)
        if parsed is not None:
            return parsed

    deployed_at = str(item.get("deployed_at") or "").strip()
    try:
        return datetime.strptime(deployed_at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _build_release_view() -> list[dict[str, Any]]:
    with RELEASE_TRACKER_LOCK:
        config = dict(release_tracker_config)
        imported = [deepcopy(item) for item in release_tracker_events]
        tracked_targets = [deepcopy(item) for item in release_tracker_targets]
    use_tracked_releases = bool(
        config.get("is_enabled") or imported or tracked_targets or str(config.get("base_path") or "").strip()
    )
    combined = imported if use_tracked_releases else [deepcopy(item) for item in RELEASES]
    combined.sort(key=_release_sort_key, reverse=True)
    return combined


def _release_step_for_board(item: dict[str, Any]) -> str:
    step = str(item.get("deployment_step_override") or item.get("deployment_step") or "").strip().upper()
    if step in {"QA", "STAGE", "PROD"}:
        return step

    folder_name = str(item.get("folder_name") or "").strip()
    file_path = str(item.get("file_path") or item.get("deployment_file_path") or "").strip()
    inferred = _infer_deployment_step(f"{folder_name} {file_path}")
    if inferred in {"QA", "STAGE", "PROD"}:
        return inferred

    environment = str(item.get("environment") or "").strip().lower()
    if environment == "development":
        return "QA"
    if environment == "staging":
        return "STAGE"
    if environment == "production":
        return "PROD"
    return ""


def _build_release_board(release_items: list[dict[str, Any]], release_targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    def ensure_row(release_key: str, fallback_name: str) -> dict[str, Any]:
        canonical_key = _canonical_release_key(release_key or fallback_name)
        row = rows.get(canonical_key)
        if row is None:
            row = {
                "release_key": release_key or fallback_name,
                "display_name": fallback_name or release_key or "Unknown Release",
                "steps": {"QA": None, "STAGE": None, "PROD": None},
                "latest_activity": datetime.min.replace(tzinfo=timezone.utc),
                "unassigned": [],
            }
            rows[canonical_key] = row
        return row

    for target in release_targets:
        folder_name = str(target.get("folder_name") or Path(str(target.get("file_path") or "")).name).strip()
        file_path = str(target.get("file_path") or "").strip()
        release_key = str(target.get("release_key_override") or "").strip() or _extract_release_reference(folder_name) or folder_name or file_path
        custom_label = str(target.get("label") or "").strip()
        row = ensure_row(release_key, custom_label or release_key or folder_name or file_path)
        if custom_label:
            row["display_name"] = custom_label

        step = _release_step_for_board(target)
        modified_dt = _parse_checked_at(str(target.get("last_modified_at") or "")) or datetime.min.replace(tzinfo=timezone.utc)
        if modified_dt > row["latest_activity"]:
            row["latest_activity"] = modified_dt

        slot = {
            "target_id": str(target.get("id") or "").strip(),
            "title": custom_label or folder_name or release_key,
            "path": file_path,
            "last_modified_at": str(target.get("last_modified_at") or "").strip(),
            "status": "available" if bool(target.get("exists", False)) else "missing",
            "folder_name": folder_name,
        }
        if step in {"QA", "STAGE", "PROD"}:
            existing_slot = row["steps"].get(step)
            existing_dt = (
                _parse_checked_at(str(existing_slot.get("last_modified_at") or "")) if isinstance(existing_slot, dict) else None
            ) or datetime.min.replace(tzinfo=timezone.utc)
            if modified_dt >= existing_dt:
                row["steps"][step] = slot
        else:
            row["unassigned"].append(slot)

    if not rows:
        for release in release_items:
            release_key = str(release.get("release_key") or release.get("version") or "").strip() or str(release.get("id") or "")
            display_name = str(release.get("name") or release_key or "Unknown Release").strip()
            row = ensure_row(release_key, display_name)
            if display_name:
                row["display_name"] = display_name

            step = _release_step_for_board(release)
            modified_dt = _release_sort_key(release)
            if modified_dt > row["latest_activity"]:
                row["latest_activity"] = modified_dt

            slot = {
                "target_id": "",
                "title": str(release.get("version") or display_name or release_key).strip(),
                "path": str(release.get("deployment_file_path") or "").strip(),
                "last_modified_at": str(release.get("deployed_at") or "").strip(),
                "status": str(release.get("status") or "available").strip(),
                "folder_name": str(release.get("version") or "").strip(),
            }
            if step in {"QA", "STAGE", "PROD"}:
                row["steps"][step] = slot
            else:
                row["unassigned"].append(slot)

    board_rows = list(rows.values())
    board_rows.sort(key=lambda item: item.get("latest_activity") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for row in board_rows:
        row["unassigned"].sort(
            key=lambda item: _parse_checked_at(str(item.get("last_modified_at") or "")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        row.pop("latest_activity", None)
    return board_rows


def _find_release_board_entry(release_board: list[dict[str, Any]], release_key: str) -> dict[str, Any] | None:
    target_key = _canonical_release_key(release_key)
    for item in release_board:
        item_key = _canonical_release_key(str(item.get("release_key") or item.get("display_name") or ""))
        if item_key == target_key:
            return item
    return None


def _build_release_notification_body(release_entry: dict[str, Any], custom_message: str) -> str:
    lines = [
        f"Release: {str(release_entry.get('display_name') or release_entry.get('release_key') or 'Unknown Release').strip()}",
    ]
    release_key = str(release_entry.get("release_key") or "").strip()
    display_name = str(release_entry.get("display_name") or "").strip()
    if release_key and release_key != display_name:
        lines.append(f"Reference: {release_key}")

    lines.append("")
    lines.append("Current board status:")
    for step in ("QA", "STAGE", "PROD"):
        slot = release_entry.get("steps", {}).get(step)
        if isinstance(slot, dict):
            slot_title = str(slot.get("title") or "Assigned").strip()
            slot_path = str(slot.get("path") or "").strip()
            suffix = f" ({slot_path})" if slot_path else ""
            lines.append(f"- {step}: {slot_title}{suffix}")
        else:
            lines.append(f"- {step}: Not assigned")

    unassigned = release_entry.get("unassigned") or []
    if isinstance(unassigned, list) and unassigned:
        lines.append("- UNASSIGNED:")
        for slot in unassigned:
            slot_title = str(slot.get("title") or "Unassigned").strip()
            slot_path = str(slot.get("path") or "").strip()
            suffix = f" ({slot_path})" if slot_path else ""
            lines.append(f"  - {slot_title}{suffix}")

    custom_message = custom_message.strip()
    if custom_message:
        lines.extend(["", "Notes:", custom_message])

    return "\n".join(lines)


def _format_notification_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return raw
    return f"{parsed.month}/{parsed.day}"


def _format_notification_time(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%H:%M")
    except ValueError:
        return raw
    hour = parsed.hour % 12 or 12
    suffix = "AM" if parsed.hour < 12 else "PM"
    if parsed.minute == 0:
        return f"{hour} {suffix}"
    return f"{hour}:{parsed.minute:02d} {suffix}"


def _parse_release_numbers(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\n,;]+", raw)
    values: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = str(part).strip()
        if not value:
            continue
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(value)
    return values


def _human_join(values: list[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _release_notification_scope_details(scope: str) -> tuple[str, str]:
    normalized = str(scope or "").strip().lower()
    if normalized == "portal":
        return "Portal Deployment team", "Production SLA Portal"
    if normalized == "both":
        return "LEAP/Portal Deployment team", "Production SLA LEAP Back Office and Portal"
    return "LEAP Deployment team", "Production SLA LEAP Back Office"


def _release_notification_scope_subject_label(scope: str) -> str:
    normalized = str(scope or "").strip().lower()
    if normalized == "portal":
        return "PORTAL"
    if normalized == "both":
        return "LEAP/PORTAL"
    return "LEAP"


def _build_structured_release_notification_body(form_data: dict[str, str]) -> str:
    release_numbers = _parse_release_numbers(str(form_data.get("release_numbers") or ""))
    release_label = _human_join(release_numbers)
    release_noun = "Release" if len(release_numbers) == 1 else "Releases"
    change_number = str(form_data.get("change_number") or "").strip()
    deployment_date = _format_notification_date(str(form_data.get("deployment_date") or ""))
    start_time = _format_notification_time(str(form_data.get("start_time") or ""))
    end_time = _format_notification_time(str(form_data.get("end_time") or ""))
    signature = str(form_data.get("signature") or "").strip()
    notes = str(form_data.get("notes") or "").strip()
    team_name, service_name = _release_notification_scope_details(str(form_data.get("deployment_scope") or ""))

    body_lines = [
        "Hello,",
        "",
        (
            f"We would like to notify you the {team_name} will migrate {release_noun} {release_label} to the PROD environment on "
            f"{deployment_date} between {start_time} - {end_time}."
        ),
        (
            f"During this migration, the {service_name} will be temporarily offline, with services expected to resume by "
            f"{end_time}."
        ),
        "This update addresses the ALM defect fixes detailed in the attached document.",
        "User Acceptance Testing (UAT) has been successfully finalized by SLA.",
        "",
        "Should you have any questions or concerns, please don't hesitate to reach out to us. Thank you!",
        f"Change Number : {change_number}",
    ]
    if notes:
        body_lines.extend(["", notes])
    if signature:
        body_lines.extend(["", signature])
    return "\n".join(body_lines)


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[A-Za-z][^>]*>", str(value or "")))


def _html_to_text(value: str) -> str:
    raw = str(value or "")
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p\s*>", "\n\n", raw)
    raw = re.sub(r"(?i)</div\s*>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def _htmlize_text_block(value: str) -> str:
    escaped = html.escape(str(value or "").strip())
    return escaped.replace("\n", "<br>")


def _build_structured_release_notification_html(form_data: dict[str, str]) -> str:
    release_numbers = _parse_release_numbers(str(form_data.get("release_numbers") or ""))
    release_label = _human_join(release_numbers)
    release_noun = "Release" if len(release_numbers) == 1 else "Releases"
    change_number = str(form_data.get("change_number") or "").strip()
    deployment_date = _format_notification_date(str(form_data.get("deployment_date") or ""))
    start_time = _format_notification_time(str(form_data.get("start_time") or ""))
    end_time = _format_notification_time(str(form_data.get("end_time") or ""))
    signature = str(form_data.get("signature") or "").strip()
    notes = str(form_data.get("notes") or "").strip()
    team_name, service_name = _release_notification_scope_details(str(form_data.get("deployment_scope") or ""))

    notes_html = ""
    if notes:
        notes_html = f"<p>{_htmlize_text_block(notes)}</p>"

    if signature:
        signature_html = signature if _looks_like_html(signature) else f"<p>{_htmlize_text_block(signature)}</p>"
    else:
        signature_html = ""

    return (
        "<html><body>"
        "<p>Hello,</p>"
        f"<p>We would like to notify you {html.escape(team_name)} will migrate "
        f"{html.escape(release_noun)} {html.escape(release_label)} to the PROD environment on "
        f"{html.escape(deployment_date)} between {html.escape(start_time)} - {html.escape(end_time)}.</p>"
        f"<p>During this migration, the {html.escape(service_name)} will be temporarily offline, "
        f"with services expected to resume by {html.escape(end_time)}.</p>"
        "<p>This update addresses the ALM defect fixes detailed in the attached document. "
        "User Acceptance Testing (UAT) has been successfully finalized by SLA.</p>"
        "<p>Should you have any questions or concerns, please don't hesitate to reach out to us. Thank you!<br>"
        f"Change Number : {html.escape(change_number)}</p>"
        f"{notes_html}"
        f"{signature_html}"
        "</body></html>"
    )


def _sync_release_tracker_once(*, force: bool = False) -> dict[str, Any]:
    with RELEASE_TRACKER_LOCK:
        config = deepcopy(release_tracker_config)
        existing_events = [deepcopy(item) for item in release_tracker_events]
        existing_targets = [deepcopy(item) for item in release_tracker_targets]

    base_path = str(config.get("base_path") or "").strip()
    if not config.get("is_enabled") and not force:
        return {"ok": True, "releases": len(existing_events), "processed": 0, "message": "Release tracker disabled"}
    if not base_path:
        with RELEASE_TRACKER_LOCK:
            release_tracker_targets[:] = []
            release_tracker_events[:] = []
            release_tracker_config["last_run_at"] = datetime.now(timezone.utc).isoformat()
            release_tracker_config["last_error"] = "Release base path is not configured."
        _save_release_tracker_targets()
        _save_release_tracker_events()
        _save_release_tracker_config()
        return {"ok": False, "releases": 0, "processed": 0, "message": "Release base path is not configured."}

    try:
        updated_targets, updated_events, processed_count = _build_release_events_from_base_path(
            base_path,
            existing_events,
            existing_targets,
        )
    except Exception as exc:  # noqa: BLE001
        with RELEASE_TRACKER_LOCK:
            release_tracker_config["last_error"] = str(exc)
            release_tracker_config["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _save_release_tracker_config()
        return {"ok": False, "releases": 0, "processed": 0, "message": str(exc)}

    with RELEASE_TRACKER_LOCK:
        release_tracker_targets[:] = updated_targets
        release_tracker_events[:] = updated_events
        release_tracker_config["last_run_at"] = datetime.now(timezone.utc).isoformat()
        release_tracker_config["last_error"] = ""

    _save_release_tracker_targets()
    _save_release_tracker_events()
    _save_release_tracker_config()
    return {
        "ok": True,
        "releases": len(updated_events),
        "processed": processed_count,
        "message": "Sync complete",
    }


def _find_server_health_check(check_id: str) -> tuple[int, dict[str, Any] | None]:
    for index, check in enumerate(server_health_checks):
        if check["id"] == check_id:
            return index, check
    return -1, None


def _find_release_tracker_target(target_id: str) -> tuple[int, dict[str, Any] | None]:
    for index, target in enumerate(release_tracker_targets):
        if str(target.get("id")) == target_id:
            return index, target
    return -1, None


def _find_release_backup_target(target_id: str, targets: list[dict[str, Any]]) -> tuple[int, dict[str, Any] | None]:
    for index, target in enumerate(targets):
        if str(target.get("id") or "").strip() == target_id:
            return index, target
    return -1, None


def _build_release_backup_target_from_form(form_data: Any, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = dict(existing or {})
    current["label"] = str(form_data.get("label", "")).strip()
    current["environment"] = _normalize_release_backup_environment(form_data.get("environment"), "QA")
    current["host"] = str(form_data.get("host", "")).strip()
    current["port"] = _coerce_int(form_data.get("port"), int(current.get("port") or 22), 1, 65_535)
    current["username"] = str(form_data.get("username", "")).strip()
    current["source_path"] = str(form_data.get("source_path", "")).strip()
    current["destination_path"] = str(form_data.get("destination_path", "")).strip()
    current["is_enabled"] = form_data.get("is_enabled") == "on"

    if (
        not str(current.get("host") or "").strip()
        or not str(current.get("username") or "").strip()
        or not str(current.get("source_path") or "").strip()
        or not str(current.get("destination_path") or "").strip()
    ):
        raise ValueError("Backup target requires host, username, source path, and destination path")

    normalized = _normalize_release_backup_target(current)
    if normalized is None:
        raise ValueError("Backup target is invalid")
    return normalized


def _build_release_backup_target_test_candidate(form_data: Any, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = dict(existing or {})
    current["label"] = str(form_data.get("label", current.get("label", ""))).strip()
    current["environment"] = _normalize_release_backup_environment(
        form_data.get("environment", current.get("environment")),
        str(current.get("environment") or "QA"),
    )
    current["host"] = str(form_data.get("host", current.get("host", ""))).strip()
    current["port"] = _coerce_int(form_data.get("port"), int(current.get("port") or 22), 1, 65_535)
    current["username"] = str(form_data.get("username", current.get("username", ""))).strip()
    current["source_path"] = str(form_data.get("source_path", current.get("source_path", ""))).strip()
    current["destination_path"] = str(form_data.get("destination_path", current.get("destination_path", ""))).strip()
    current["is_enabled"] = form_data.get("is_enabled") == "on" if "is_enabled" in form_data else bool(current.get("is_enabled", True))

    if not str(current.get("host") or "").strip() or not str(current.get("username") or "").strip():
        raise ValueError("Backup target test requires host and username")

    normalized = _normalize_release_backup_target(current)
    if normalized is None:
        raise ValueError("Backup target test is invalid")
    return normalized


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
        "release-backup-target-added": "Release backup target added.",
        "release-backup-target-updated": "Release backup target updated.",
        "release-backup-target-deleted": "Release backup target removed.",
        "release-backup-target-missing": "Release backup target was not found.",
        "release-backup-test-passed": "Release backup SSH connection passed.",
        "release-backup-test-failed": "Release backup SSH connection failed.",
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

        bottom_push = max(0.0, math.sin(angle)) * (110.0 + max_local_radius * 0.12)
        center_x = mainframe_x0 + orbit_x * math.cos(angle)
        center_y = mainframe_y0 + orbit_y * math.sin(angle) + bottom_push
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

    recipients = _health_alert_recipients(str(check.get("alert_recipients") or ""))
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
    check_group = str(check.get("server_group") or SERVER_GROUP_DEFAULT)
    http_status = current_result.get("http_status")
    response_ms = current_result.get("response_ms")
    checked_at = current_result.get("checked_at")
    error_message = current_result.get("error") or ""
    observed_status = str(http_status) if http_status is not None else "N/A"
    expected_status = str(current_result.get("expected_status"))
    safe_name = html.escape(check_name)
    safe_group = html.escape(check_group)
    safe_url = html.escape(check_url)
    safe_checked_at = html.escape(str(checked_at))
    safe_response_ms = html.escape(str(response_ms))
    safe_observed = html.escape(observed_status)
    safe_expected = html.escape(expected_status)
    safe_error = html.escape(str(error_message))

    if alert_kind == "down":
        subject = f"{EMAIL_SUBJECT_PREFIX} DOWN - {check_name}"
        body = (
            f"Server health check is DOWN.\n\n"
            f"Check: {check_name}\n"
            f"Group: {check_group}\n"
            f"URL: {check_url}\n"
            f"Expected status: {current_result.get('expected_status')}\n"
            f"Observed status: {http_status if http_status is not None else 'N/A'}\n"
            f"Response time: {response_ms} ms\n"
            f"Checked at (UTC): {checked_at}\n"
        )
        if error_message:
            body += f"Error: {error_message}\n"

        html_body = (
            "<html><body style=\"font-family:Segoe UI,Arial,sans-serif;color:#0f172a;\">"
            "<h2 style=\"margin:0 0 12px;color:#b91c1c;\">Server Health Alert: DOWN</h2>"
            "<table style=\"border-collapse:collapse;font-size:14px;\">"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Check</b></td><td>{safe_name}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Group</b></td><td>{safe_group}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>URL</b></td><td><a href=\"{safe_url}\">{safe_url}</a></td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Expected</b></td><td>HTTP {safe_expected}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Observed</b></td><td>HTTP {safe_observed}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Response</b></td><td>{safe_response_ms} ms</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Checked (UTC)</b></td><td>{safe_checked_at}</td></tr>"
            "</table>"
            + (f"<p style=\"margin-top:12px;\"><b>Error:</b> {safe_error}</p>" if error_message else "")
            + "</body></html>"
        )
    else:
        subject = f"{EMAIL_SUBJECT_PREFIX} RECOVERED - {check_name}"
        body = (
            f"Server health check has recovered.\n\n"
            f"Check: {check_name}\n"
            f"Group: {check_group}\n"
            f"URL: {check_url}\n"
            f"Observed status: {http_status if http_status is not None else 'N/A'}\n"
            f"Response time: {response_ms} ms\n"
            f"Checked at (UTC): {checked_at}\n"
        )
        html_body = (
            "<html><body style=\"font-family:Segoe UI,Arial,sans-serif;color:#0f172a;\">"
            "<h2 style=\"margin:0 0 12px;color:#15803d;\">Server Health Alert: RECOVERED</h2>"
            "<table style=\"border-collapse:collapse;font-size:14px;\">"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Check</b></td><td>{safe_name}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Group</b></td><td>{safe_group}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>URL</b></td><td><a href=\"{safe_url}\">{safe_url}</a></td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Observed</b></td><td>HTTP {safe_observed}</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Response</b></td><td>{safe_response_ms} ms</td></tr>"
            f"<tr><td style=\"padding:4px 10px 4px 0;\"><b>Checked (UTC)</b></td><td>{safe_checked_at}</td></tr>"
            "</table>"
            "</body></html>"
        )

    sent, send_error = _send_alert_email(subject, body, recipients, html_body=html_body)
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

    if email_alerts_enabled:
        alert_recipients = alert_recipients or DEFAULT_HEALTH_ALERT_RECIPIENTS_RAW

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
        "email_alerts_initialized": True,
        "alert_recipients": alert_recipients,
        "alert_on_recovery": alert_on_recovery,
        "last_alert": existing.get("last_alert") if existing else None,
        "last_check": existing.get("last_check") if existing else None,
        "total_checks": int(existing.get("total_checks") or 0) if existing else 0,
        "successful_checks": int(existing.get("successful_checks") or 0) if existing else 0,
    }


server_health_checks: list[dict[str, Any]] = _load_server_health_checks()
release_tracker_config: dict[str, Any] = _load_release_tracker_config()
release_tracker_targets: list[dict[str, Any]] = _load_release_tracker_targets()
release_tracker_events: list[dict[str, Any]] = _load_release_tracker_events()


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


def _background_release_tracker_loop() -> None:
    while True:
        interval_seconds = int(RELEASE_TRACKER_DEFAULTS["poll_interval_seconds"])
        try:
            _sync_release_tracker_once(force=False)
            with RELEASE_TRACKER_LOCK:
                interval_seconds = _coerce_int(
                    release_tracker_config.get("poll_interval_seconds"),
                    int(RELEASE_TRACKER_DEFAULTS["poll_interval_seconds"]),
                    30,
                    86_400,
                )
        except Exception:  # noqa: BLE001
            pass
        sleep(interval_seconds)


def _start_background_release_tracker() -> None:
    global _release_tracker_thread

    if app.config.get("TESTING"):
        return

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    with _release_tracker_start_lock:
        if _release_tracker_thread is not None and _release_tracker_thread.is_alive():
            return

        _release_tracker_thread = threading.Thread(
            target=_background_release_tracker_loop,
            name="release-tracker-poller",
            daemon=True,
        )
        _release_tracker_thread.start()


@app.before_request
def ensure_background_checker_started() -> None:
    _start_background_health_checker()
    _start_background_release_tracker()


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
    _start_background_release_tracker()
    release_items = _build_release_view()
    with RELEASE_TRACKER_LOCK:
        tracker_snapshot = dict(release_tracker_config)
        targets_snapshot = [dict(item) for item in release_tracker_targets if bool(item.get("is_enabled", True))]
    release_board = _build_release_board(release_items, targets_snapshot)
    release_backup_state = _build_release_backup_state(tracker_snapshot)

    notice_code = request.args.get("notice")
    notice_message = str(request.args.get("message") or "").strip()
    releases_count = _coerce_int(request.args.get("releases"), 0, 0, 1_000_000)
    processed_count = _coerce_int(request.args.get("processed"), 0, 0, 1_000_000)
    release_notice = ""
    if notice_code == "release-synced":
        release_notice = (
            f"Release folder scan complete. Checked {processed_count} folder(s) and built {releases_count} tracked release item(s)."
        )
    elif notice_code == "release-sync-error":
        release_notice = "Release folder scan failed. Check tracker error details below."
    elif notice_code == "release-config-saved":
        release_notice = "Release folder tracker configuration saved."
    elif notice_code == "release-target-renamed":
        release_notice = "Release name updated."
    elif notice_code == "release-target-ignored":
        release_notice = "Release folder ignored."
    elif notice_code == "release-target-restored":
        release_notice = "Release folder restored."
    elif notice_code == "release-notification-sent":
        release_notice = "Release notification email sent."
    elif notice_code == "release-notification-error":
        release_notice = notice_message or "Release notification email failed."
    elif notice_code == "release-backup-complete":
        release_notice = notice_message or "Non-prod backup completed."
    elif notice_code == "release-backup-error":
        release_notice = notice_message or "Non-prod backup failed."

    return render_template(
        "releases.html",
        page_title="Releases",
        active_page="releases",
        release_board=release_board,
        release_notice=release_notice,
        release_tracker=tracker_snapshot,
        release_targets=targets_snapshot,
        tracked_release_count=len(release_board),
        smtp_configured=_smtp_is_configured(),
        release_notification_defaults={
            "to_recipients": str(tracker_snapshot.get("notification_to_recipients") or "").strip(),
            "cc_recipients": str(tracker_snapshot.get("notification_cc_recipients") or "").strip(),
            "signature": str(tracker_snapshot.get("notification_signature_html") or ""),
            "subject_prefix": RELEASE_NOTIFICATION_SUBJECT_PREFIX,
        },
        release_backup=release_backup_state,
        stats={
            "qa": sum(1 for item in release_board if item["steps"].get("QA")),
            "stage": sum(1 for item in release_board if item["steps"].get("STAGE")),
            "prod": sum(1 for item in release_board if item["steps"].get("PROD")),
        },
    )


@app.post("/config/releases/update")
def update_release_tracker_config() -> Any:
    with RELEASE_TRACKER_LOCK:
        existing = dict(release_tracker_config)

    updated = dict(existing)
    updated["provider"] = "file_paths"
    updated["base_path"] = str(request.form.get("base_path", "")).strip()
    updated["is_enabled"] = request.form.get("is_enabled") == "on"
    updated["poll_interval_seconds"] = _coerce_int(request.form.get("poll_interval_seconds"), 180, 30, 86_400)
    updated["notification_signature_html"] = str(request.form.get("notification_signature_html", ""))

    normalized = _normalize_release_tracker_config(updated)
    with RELEASE_TRACKER_LOCK:
        release_tracker_config.update(normalized)
    _save_release_tracker_config()
    _sync_release_tracker_once(force=True)

    return redirect(url_for("server_health_config", notice="release-config-saved"))


@app.post("/config/releases/backup-targets/add")
def add_release_backup_target() -> Any:
    with RELEASE_TRACKER_LOCK:
        existing_targets = [deepcopy(item) for item in release_tracker_config.get("backup_targets") or [] if isinstance(item, dict)]

    try:
        new_target = _build_release_backup_target_from_form(request.form)
    except ValueError:
        return redirect(url_for("server_health_config", notice="missing-required"))

    backup_password = str(request.form.get("password", ""))
    if backup_password:
        _upsert_env_value(str(new_target.get("password_env_key") or ""), backup_password)

    existing_targets.append(new_target)
    with RELEASE_TRACKER_LOCK:
        release_tracker_config["backup_targets"] = [
            _normalize_release_backup_target(item, fallback_index=index)
            for index, item in enumerate(existing_targets, start=1)
            if _normalize_release_backup_target(item, fallback_index=index) is not None
        ]
    _save_release_tracker_config()
    return redirect(url_for("server_health_config", notice="release-backup-target-added"))


@app.post("/config/releases/backup-targets/test")
def test_new_release_backup_target() -> Any:
    try:
        target = _build_release_backup_target_test_candidate(request.form)
    except ValueError:
        return redirect(url_for("server_health_config", notice="missing-required"))

    password_value = str(request.form.get("password", ""))
    password_override = password_value if password_value else None
    ok, message = _test_release_backup_target_connection(target, password_override=password_override)
    return redirect(
        url_for(
            "server_health_config",
            notice="release-backup-test-passed" if ok else "release-backup-test-failed",
            message=message,
        )
    )


@app.post("/config/releases/backup-targets/<target_id>/update")
def update_release_backup_target(target_id: str) -> Any:
    with RELEASE_TRACKER_LOCK:
        existing_targets = [deepcopy(item) for item in release_tracker_config.get("backup_targets") or [] if isinstance(item, dict)]
    index, existing_target = _find_release_backup_target(target_id, existing_targets)
    if existing_target is None:
        return redirect(url_for("server_health_config", notice="release-backup-target-missing"))

    try:
        updated_target = _build_release_backup_target_from_form(request.form, existing=existing_target)
    except ValueError:
        return redirect(url_for("server_health_config", notice="missing-required"))

    clear_password = request.form.get("clear_password") == "on"
    password_value = str(request.form.get("password", ""))
    password_env_key = str(updated_target.get("password_env_key") or "")
    if clear_password:
        _delete_env_value(password_env_key)
    elif password_value:
        _upsert_env_value(password_env_key, password_value)

    existing_targets[index] = updated_target
    with RELEASE_TRACKER_LOCK:
        release_tracker_config["backup_targets"] = [
            _normalize_release_backup_target(item, fallback_index=position)
            for position, item in enumerate(existing_targets, start=1)
            if _normalize_release_backup_target(item, fallback_index=position) is not None
        ]
    _save_release_tracker_config()
    return redirect(url_for("server_health_config", notice="release-backup-target-updated"))


@app.post("/config/releases/backup-targets/<target_id>/test")
def test_saved_release_backup_target(target_id: str) -> Any:
    with RELEASE_TRACKER_LOCK:
        existing_targets = [deepcopy(item) for item in release_tracker_config.get("backup_targets") or [] if isinstance(item, dict)]
    _, existing_target = _find_release_backup_target(target_id, existing_targets)
    if existing_target is None:
        return redirect(url_for("server_health_config", notice="release-backup-target-missing"))

    try:
        target = _build_release_backup_target_test_candidate(request.form, existing=existing_target)
    except ValueError:
        return redirect(url_for("server_health_config", notice="missing-required"))

    password_override: str | None = None
    if request.form.get("clear_password") == "on":
        password_override = ""
    password_value = str(request.form.get("password", ""))
    if password_value:
        password_override = password_value

    ok, message = _test_release_backup_target_connection(target, password_override=password_override)
    return redirect(
        url_for(
            "server_health_config",
            notice="release-backup-test-passed" if ok else "release-backup-test-failed",
            message=message,
        )
    )


@app.post("/config/releases/backup-targets/<target_id>/delete")
def delete_release_backup_target(target_id: str) -> Any:
    with RELEASE_TRACKER_LOCK:
        existing_targets = [deepcopy(item) for item in release_tracker_config.get("backup_targets") or [] if isinstance(item, dict)]
    index, existing_target = _find_release_backup_target(target_id, existing_targets)
    if existing_target is None:
        return redirect(url_for("server_health_config", notice="release-backup-target-missing"))

    password_env_key = str(existing_target.get("password_env_key") or "")
    if password_env_key:
        _delete_env_value(password_env_key)

    del existing_targets[index]
    with RELEASE_TRACKER_LOCK:
        release_tracker_config["backup_targets"] = [
            _normalize_release_backup_target(item, fallback_index=position)
            for position, item in enumerate(existing_targets, start=1)
            if _normalize_release_backup_target(item, fallback_index=position) is not None
        ]
    _save_release_tracker_config()
    return redirect(url_for("server_health_config", notice="release-backup-target-deleted"))


@app.post("/releases/sync")
def sync_releases_now() -> Any:
    result = _sync_release_tracker_once(force=True)
    if result.get("ok"):
        return redirect(
            url_for(
                "releases",
                notice="release-synced",
                releases=_coerce_int(result.get("releases"), 0, 0, 1_000_000),
                processed=_coerce_int(result.get("processed"), 0, 0, 1_000_000),
            )
        )
    return redirect(url_for("releases", notice="release-sync-error"))


@app.post("/releases/notify")
def send_release_notification() -> Any:
    to_recipients_raw = str(request.form.get("to_recipients", request.form.get("recipients", ""))).strip()
    cc_recipients_raw = str(request.form.get("cc_recipients", "")).strip()
    custom_subject = str(request.form.get("subject", "")).strip()
    release_numbers = _parse_release_numbers(str(request.form.get("release_numbers", "")).strip())
    change_number = str(request.form.get("change_number", "")).strip()
    deployment_scope = str(request.form.get("deployment_scope", "leap")).strip().lower()
    deployment_date = str(request.form.get("deployment_date", "")).strip()
    start_time = str(request.form.get("start_time", "")).strip()
    end_time = str(request.form.get("end_time", "")).strip()
    notes = str(request.form.get("notes", "")).strip()
    signature = str(request.form.get("signature", "")).strip()
    to_recipients = _parse_recipients(to_recipients_raw)
    cc_recipients = _parse_recipients(cc_recipients_raw)
    uploaded_files = [item for item in request.files.getlist("attachments") if item and str(item.filename or "").strip()]

    if (
        not to_recipients
        or not release_numbers
        or not change_number
        or not deployment_date
        or not start_time
        or not end_time
        or deployment_scope not in {"leap", "portal", "both"}
    ):
        return redirect(
            url_for(
                "releases",
                notice="release-notification-error",
                message="Release number, change number, deployment type, date, time window, and To recipients are required.",
            )
        )

    _save_release_notification_defaults(to_recipients_raw, cc_recipients_raw)

    attachment_payloads: list[dict[str, Any]] = []
    for item in uploaded_files:
        try:
            content = item.read()
        except OSError:
            return redirect(
                url_for(
                    "releases",
                    notice="release-notification-error",
                    message=f"Could not read attachment {item.filename}.",
                )
            )
        if not content:
            continue
        attachment_payloads.append({"filename": str(item.filename).strip(), "content": content})

    subject_label = _release_notification_scope_subject_label(deployment_scope)
    subject_release_prefix = "Release" if len(release_numbers) == 1 else "Releases"
    subject_release_value = ", ".join(release_numbers)
    subject = custom_subject or f"{subject_release_prefix} {subject_release_value}({subject_label}) - PROD release"
    notification_payload = {
        "release_numbers": ", ".join(release_numbers),
        "change_number": change_number,
        "deployment_scope": deployment_scope,
        "deployment_date": deployment_date,
        "start_time": start_time,
        "end_time": end_time,
        "notes": notes,
        "signature": _html_to_text(signature) if _looks_like_html(signature) else signature,
    }
    body = _build_structured_release_notification_body(notification_payload)
    html_body = _build_structured_release_notification_html(
        {
            **notification_payload,
            "signature": signature,
        }
    )
    sent, send_error = _send_alert_email(
        subject,
        body,
        to_recipients,
        cc_recipients=cc_recipients,
        html_body=html_body,
        attachments=attachment_payloads,
    )
    if not sent:
        return redirect(
            url_for(
                "releases",
                notice="release-notification-error",
                message=send_error or "Release notification email failed.",
            )
        )

    return redirect(url_for("releases", notice="release-notification-sent"))


@app.post("/releases/backup")
def run_release_backup() -> Any:
    backup_scope = str(request.form.get("backup_scope", "all")).strip().lower()
    backup_label = str(request.form.get("backup_label", "")).strip()
    backup_ok, backup_message, backup_results = _run_release_backups(environment=backup_scope, backup_label=backup_label)
    _record_release_backup_batch_result(results=backup_results, summary_message=backup_message, is_ok=backup_ok)
    if not backup_ok:
        return redirect(url_for("releases", notice="release-backup-error", message=backup_message))
    return redirect(url_for("releases", notice="release-backup-complete", message=backup_message))


@app.post("/config/releases/targets/<target_id>/rename")
def rename_release_target(target_id: str) -> Any:
    new_label = str(request.form.get("label", "")).strip()
    with RELEASE_TRACKER_LOCK:
        index, target = _find_release_tracker_target(target_id)
        if target is None:
            return redirect(url_for("releases"))
        updated_target = dict(target)
        updated_target["label"] = new_label
        release_tracker_targets[index] = _normalize_release_tracker_target(updated_target) or updated_target
    _save_release_tracker_targets()
    _sync_release_tracker_once(force=True)
    return redirect(url_for("releases", notice="release-target-renamed"))


@app.post("/api/releases/targets/<target_id>/step")
def update_release_target_step(target_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    requested_step = str(payload.get("deployment_step") or "").strip().upper()
    if requested_step not in {"QA", "STAGE", "PROD"}:
        return jsonify({"ok": False, "message": "Invalid deployment step"}), 400

    with RELEASE_TRACKER_LOCK:
        index, target = _find_release_tracker_target(target_id)
        if target is None:
            return jsonify({"ok": False, "message": "Release target not found"}), 404
        updated_target = dict(target)
        updated_target["deployment_step_override"] = requested_step
        release_tracker_targets[index] = _normalize_release_tracker_target(updated_target) or updated_target

    _save_release_tracker_targets()
    result = _sync_release_tracker_once(force=True)
    if not result.get("ok"):
        return jsonify({"ok": False, "message": str(result.get("message") or "Could not update deployment step")}), 500
    return jsonify({"ok": True, "deployment_step": requested_step})


@app.post("/config/releases/targets/<target_id>/toggle-ignore")
def toggle_release_target_ignore(target_id: str) -> Any:
    with RELEASE_TRACKER_LOCK:
        index, target = _find_release_tracker_target(target_id)
        if target is None:
            return redirect(url_for("releases"))
        updated_target = dict(target)
        updated_target["is_enabled"] = not bool(target.get("is_enabled", True))
        release_tracker_targets[index] = _normalize_release_tracker_target(updated_target) or updated_target
        is_enabled = bool(release_tracker_targets[index].get("is_enabled", True))
    _save_release_tracker_targets()
    _sync_release_tracker_once(force=True)
    notice = "release-target-restored" if is_enabled else "release-target-ignored"
    return redirect(url_for("releases", notice=notice))


@app.get("/sla-payments")
def payments() -> str:
    return render_template(
        "sla_payments.html",
        page_title="SLA Payments",
        active_page="sla-payments",
        automation={
            "base_dir": str(BASE_DIR),
            "attachments_dir": str(ATTACHMENTS_DIR),
            "backup_dir": str(BACKUP_DIR),
            "new_json_dir": str(NEW_JSON_DIR),
            "log_dir": str(LOG_DIR),
            "bad_transactions": sorted(BAD_TRANSACTIONS),
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
    with RELEASE_TRACKER_LOCK:
        release_tracker_snapshot = dict(release_tracker_config)
        release_targets_snapshot = [dict(item) for item in release_tracker_targets if bool(item.get("is_enabled", True))]
    release_backup_state = _build_release_backup_state(release_tracker_snapshot)

    for check in checks_snapshot:
        checks_for_view.append(
            {
                **check,
                "has_password_secret": _has_secret(check["password_env_key"]),
                "has_bearer_secret": _has_secret(check["bearer_token_env_key"]),
            }
        )
    release_board = _build_release_board(_build_release_view(), release_targets_snapshot)
    notice_code = request.args.get("notice")
    notice_text = _notice_text(
        notice_code,
        added=request.args.get("added"),
        skipped=request.args.get("skipped"),
    )
    notice_message = str(request.args.get("message") or "").strip()
    if not notice_text and notice_code == "release-config-saved":
        notice_text = "Release tracker and backup configuration saved."
    elif not notice_text and notice_code in {"release-backup-test-passed", "release-backup-test-failed"}:
        notice_text = notice_message or _notice_text(notice_code)

    return render_template(
        "config_server_health.html",
        page_title="Server Health Config",
        active_page="config-server-health",
        checks=checks_for_view,
        release_tracker=release_tracker_snapshot,
        release_tracker_stats={
            "discovered_folders": len(release_targets_snapshot),
            "tracked_releases": len(release_board),
        },
        server_group_options=SERVER_GROUP_OPTIONS,
        notice_text=notice_text,
        health_config_stats=_server_health_stats(),
        health_check_interval_seconds=HEALTH_CHECK_INTERVAL_SECONDS,
        smtp_configured=_smtp_is_configured(),
        env_path=str(ENV_PATH),
        release_backup=release_backup_state,
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

    if email_alerts_enabled:
        alert_recipients = alert_recipients or DEFAULT_HEALTH_ALERT_RECIPIENTS_RAW

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
                    "email_alerts_initialized": True,
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


@app.post("/api/payments/run-email-date")
def run_payment_email_date_automation():
    payload = request.get_json(silent=True) or {}
    try:
        report_date = parse_report_date(payload.get("report_date"))
        result = SLAPaymentAutomationRunner().run_from_email_date(report_date)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except AutomationDependencyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except Exception as exc:
        app.logger.exception("SLA payment email-date automation failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(result)


@app.post("/api/payments/run-app-ids")
def run_payment_app_id_automation():
    payload = request.get_json(silent=True) or {}
    app_ids = parse_app_ids(payload.get("app_ids"))
    if not app_ids:
        return jsonify({"ok": False, "error": "Enter at least one valid application ID."}), 400

    try:
        result = SLAPaymentAutomationRunner().run_from_app_ids(app_ids)
    except AutomationDependencyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except Exception as exc:
        app.logger.exception("SLA payment App ID automation failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
