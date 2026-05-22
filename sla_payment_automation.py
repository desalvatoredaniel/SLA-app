from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from importlib import import_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


DEFAULT_BASE_DIR = Path(__file__).resolve().parent / "instance" / "sla_payment_automation"

BASE_DIR = DEFAULT_BASE_DIR
ATTACHMENTS_DIR = BASE_DIR / "attachments"
BACKUP_DIR = BASE_DIR / "json_back_up"
NEW_JSON_DIR = BASE_DIR / "new_json"
LOG_DIR = BASE_DIR / "logs"
DATABASE_MODE = "read_only_selects_only"
OUTLOOK_CONNECT_MODE = "dispatch"
OUTLOOK_HELPER_TIMEOUT_SECONDS = 12

BAD_TRANSACTIONS = {
    "050625C19-CAC0351F-4D17-4132-AC05-D388CBEEB25A",
}

APP_ID_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,80}$")
TRANSACTION_PARAM_NAMES = (
    "transaction_id",
    "transactionid",
    "transactionId",
    "req_transaction_uuid",
    "request_token",
    "requestID",
)


class AutomationDependencyError(RuntimeError):
    pass


ProgressCallback = Callable[[str, str, dict[str, Any] | None], None]


def emit_progress(
    progress: ProgressCallback | None,
    level: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    if progress is not None:
        progress(level, message, details or {})


def ensure_payment_dirs() -> None:
    for folder in (ATTACHMENTS_DIR, BACKUP_DIR, NEW_JSON_DIR, LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def configure_payment_logging() -> None:
    ensure_payment_dirs()
    logger = logging.getLogger()
    if any(
        isinstance(handler, logging.FileHandler) and str(LOG_DIR / "sla_payment.log") == handler.baseFilename
        for handler in logger.handlers
    ):
        return

    handler = logging.FileHandler(LOG_DIR / "sla_payment.log", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def parse_app_ids(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        raw_text = "\n".join(str(item) for item in raw_value)
    else:
        raw_text = str(raw_value or "")

    app_ids: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", raw_text):
        app_id = token.strip()
        if not app_id or not APP_ID_TOKEN_PATTERN.fullmatch(app_id):
            continue

        dedupe_key = app_id.upper()
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        app_ids.append(app_id)

    return app_ids


def parse_report_date(raw_value: Any) -> datetime:
    value = str(raw_value or "").strip()
    if not value:
        return datetime.today() - timedelta(days=1)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    raise ValueError("Report date must be YYYY-MM-DD or MM/DD/YYYY.")


def build_report_subject(report_date: datetime) -> str:
    date_str = report_date.strftime("%m/%d/%Y")
    return (
        f"PROD: SLA Payment Reports "
        f"({date_str} 12:00:00 AM - {date_str} 11:59:59 PM)"
    )


def import_required_modules(*names: str) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    missing: list[str] = []
    for name in names:
        try:
            modules[name] = import_module(name)
        except ImportError:
            missing.append(name)

    if missing:
        raise AutomationDependencyError(
            "Missing automation dependency: "
            + ", ".join(missing)
            + ". Install/run this on the Windows automation host with Outlook, SQL Server, Oracle, pandas, and db_config available."
        )

    return modules


def extract_transaction_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in TRANSACTION_PARAM_NAMES:
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return None


def row_url(row: Any) -> str | None:
    if row is None:
        return None

    try:
        value = row[5]
    except (IndexError, TypeError):
        value = None

    return str(value) if value else None


class JSONProcessor:
    def __init__(self, input_file: Path, output_file: Path):
        self.input_file = Path(input_file)
        self.output_file = Path(output_file)
        self.data: dict[str, Any] | None = None

    def read_json(self) -> dict[str, Any]:
        if not self.input_file.exists():
            logging.warning("JSON file not found: %s", self.input_file)
            self.data = {}
            return self.data

        if self.input_file.stat().st_size == 0:
            logging.warning("JSON file is empty: %s", self.input_file)
            self.data = {}
            return self.data

        try:
            with self.input_file.open("r", encoding="utf-8") as file:
                self.data = json.load(file)
        except json.JSONDecodeError as exc:
            logging.error("Invalid JSON in %s: %s", self.input_file, exc)
            self.data = {}

        return self.data

    def process_json(self) -> dict[str, Any] | None:
        if not self.data:
            return self.data

        sections = self.data.get("Sections", {})
        self.clean_principals(sections)
        self.clean_personal_questionnaire(sections)
        return self.data

    def clean_principals(self, sections: dict[str, Any]) -> None:
        principal_section = sections.get("Principal", {})
        principals = principal_section.get("principals", [])
        principal_section["principals"] = [
            principal
            for principal in principals
            if isinstance(principal, dict) and "principalId" in principal
        ]

    def clean_personal_questionnaire(self, sections: dict[str, Any]) -> None:
        questionnaire_section = sections.get("Personal_Questionnaire", {})
        principal_info = questionnaire_section.get("principalInfo", [])
        cleaned_principal_info = []

        for person in principal_info:
            if not isinstance(person, dict):
                continue

            first_name = person.get("firstName")
            if first_name is None:
                continue
            if isinstance(first_name, str) and first_name.strip() == "":
                continue

            cleaned_principal_info.append(person)

        questionnaire_section["principalInfo"] = cleaned_principal_info

    def write_json(self) -> None:
        if self.data is None:
            raise ValueError("No data loaded. Cannot write JSON.")

        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        with self.output_file.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, indent=4)

    def execute(self) -> dict[str, Any] | None:
        self.read_json()
        self.process_json()
        self.write_json()
        logging.info("Processed JSON written to %s", self.output_file)
        return self.data


class SLAPayment:
    def __init__(
        self,
        sql_conn: Any,
        oracle_conn: Any,
        *,
        transaction_id: str | None = None,
        old_app_number: str | None = None,
    ):
        self.transaction_id = transaction_id
        self.sql_conn = sql_conn
        self.oracle_conn = oracle_conn
        self.base_url: str | None = None
        self.old_app_number = old_app_number
        self.app_number: str | None = None
        self.json_backup: str | None = None
        self.backup_path: Path | None = None
        self.clean_json_path: Path | None = None
        self.status = "pending"
        self.message = ""

    def get_base_url_by_transaction(self) -> str | None:
        if not self.transaction_id:
            return None

        query = """
            SELECT *
            FROM [Prod_SharedServices].[metrics].[Request]
            WHERE url LIKE ?
        """

        with self.sql_conn.cursor() as cursor:
            cursor.execute(query, f"%{self.transaction_id}%")
            row = cursor.fetchone()

        if not row:
            logging.warning("No URL found for transaction %s", self.transaction_id)
            self.base_url = "no response"
            return None

        self.base_url = row_url(row)
        return self.base_url

    def get_base_url_by_app_id(self) -> str | None:
        if not self.old_app_number:
            return None

        query = """
            SELECT *
            FROM [Prod_SharedServices].[metrics].[Request]
            WHERE url LIKE ?
        """

        with self.sql_conn.cursor() as cursor:
            cursor.execute(query, f"%merchant_defined_data1={self.old_app_number}%")
            row = cursor.fetchone()

        if not row:
            logging.warning("No payment response URL found for app id %s", self.old_app_number)
            self.base_url = "no response"
            return None

        self.base_url = row_url(row)
        if self.base_url and not self.transaction_id:
            self.transaction_id = extract_transaction_id_from_url(self.base_url)
        return self.base_url

    def extract_old_app_number(self) -> str | None:
        if self.old_app_number:
            return self.old_app_number

        if not self.base_url or self.base_url == "no response":
            return None

        key = "merchant_defined_data1="
        start = self.base_url.find(key)
        if start == -1:
            logging.warning("No merchant_defined_data1 found for %s", self.transaction_id)
            return None

        start += len(key)
        end = self.base_url.find("&", start)
        self.old_app_number = self.base_url[start:] if end == -1 else self.base_url[start:end]
        return self.old_app_number

    def get_json_backup(self) -> str | None:
        if not self.old_app_number:
            return None

        query = """
            SELECT APPLICATION_JSON
            FROM BFLOWNER.APPLICATION
            WHERE OLD_APPLICATION_NUMBER = :old_app_number
        """

        with self.oracle_conn.cursor() as cursor:
            cursor.execute(query, old_app_number=self.old_app_number)
            row = cursor.fetchone()

        if not row:
            logging.warning("No JSON found for old app number %s", self.old_app_number)
            self.json_backup = "failed"
            return None

        self.json_backup = row[0].read()
        self.backup_path = BACKUP_DIR / f"{self.old_app_number}_back_up.json"
        self.backup_path.write_text(self.json_backup, encoding="utf-8")

        self.clean_json_path = NEW_JSON_DIR / f"{self.old_app_number}.json"
        JSONProcessor(input_file=self.backup_path, output_file=self.clean_json_path).execute()
        return self.json_backup

    def get_app_number(self) -> str | None:
        if not self.old_app_number:
            return None

        query = """
            SELECT APPLICATION_NUMBER
            FROM BFLOWNER.APPLICATION
            WHERE OLD_APPLICATION_NUMBER = :old_app_number
        """

        with self.oracle_conn.cursor() as cursor:
            cursor.execute(query, old_app_number=self.old_app_number)
            row = cursor.fetchone()

        if row:
            self.app_number = row[0]

        return self.app_number

    def process_from_transaction(self) -> "SLAPayment":
        self.get_base_url_by_transaction()
        self.extract_old_app_number()
        self.get_json_backup()
        self.get_app_number()
        self.status = "processed" if self.app_number or self.json_backup else "missing"
        return self

    def process_from_app_id(self) -> "SLAPayment":
        self.get_base_url_by_app_id()
        self.get_json_backup()
        self.get_app_number()
        self.status = "processed" if self.app_number or self.json_backup else "missing"
        return self

    def to_log_dict(self) -> dict[str, Any]:
        cleaned_url = ""
        if self.base_url and self.base_url != "no response":
            cleaned_url = self.base_url.replace(
                "http://sharedservices.ny.gov/api/payment/response?",
                "",
            )

        clean_json_content = ""
        if self.clean_json_path and self.clean_json_path.exists():
            clean_json_content = self.clean_json_path.read_text(encoding="utf-8")

        return {
            "Transaction ID": self.transaction_id,
            "Old App Number": self.old_app_number,
            "App Number": self.app_number,
            "Base URL": cleaned_url,
            "Status": self.status,
            "Backup JSON Path": str(self.backup_path) if self.backup_path else "",
            "Clean JSON Path": str(self.clean_json_path) if self.clean_json_path else "",
            "Clean JSON": clean_json_content,
            "Database Mode": DATABASE_MODE,
        }


class OutlookEmailReader:
    def __init__(self, attachment_folder_path: Path, progress: ProgressCallback | None = None):
        emit_progress(progress, "info", "Initializing Outlook COM connection.")
        emit_progress(progress, "info", "Loading win32com.client module.")
        modules = import_required_modules("win32com.client")
        win32_client = modules["win32com.client"]
        emit_progress(progress, "info", "win32com.client module loaded.")
        self.outlook_app = self._connect_outlook_app(win32_client, progress)
        emit_progress(progress, "info", "Opening Outlook MAPI namespace.")
        self.namespace = self.outlook_app.GetNamespace("MAPI")
        emit_progress(progress, "info", "Outlook MAPI namespace opened.")
        emit_progress(progress, "info", "Opening default Outlook inbox.")
        self.inbox = self.namespace.GetDefaultFolder(6)
        self.attachment_folder_path = Path(attachment_folder_path)
        emit_progress(progress, "info", "Outlook inbox initialized.")

    def _connect_outlook_app(self, win32_client: Any, progress: ProgressCallback | None = None) -> Any:
        mode = OUTLOOK_CONNECT_MODE if OUTLOOK_CONNECT_MODE in {"active", "dispatch", "auto"} else "dispatch"
        emit_progress(progress, "info", "Using Outlook COM connection mode.", {"mode": mode})

        if mode in {"active", "auto"}:
            try:
                emit_progress(progress, "info", "Connecting to active Outlook.Application COM object.")
                outlook_app = win32_client.GetActiveObject("Outlook.Application")
                emit_progress(progress, "success", "Connected to active Outlook.Application COM object.")
                return outlook_app
            except Exception as exc:
                logging.warning("Could not connect to active Outlook COM object: %s", exc)
                emit_progress(progress, "warning", "Could not connect to active Outlook.Application.", {"error": str(exc)})
                if mode == "active":
                    raise RuntimeError(
                        "Could not connect to a running Classic Outlook instance. "
                        "Open Classic Outlook with the correct profile in this same Windows user session, "
                        "dismiss any login/security/profile prompts, then run the automation again."
                    ) from exc

        emit_progress(progress, "info", "Dispatching Outlook.Application COM object using original script behavior.")
        outlook_app = win32_client.Dispatch("Outlook.Application")
        emit_progress(progress, "info", "Outlook.Application COM object dispatched.")
        return outlook_app

    def retrieve_attachments(self, report_date: datetime | None = None, progress: ProgressCallback | None = None) -> list[Path]:
        if report_date is None:
            report_date = datetime.today() - timedelta(days=1)

        subject_text = build_report_subject(report_date)
        logging.info("Searching Outlook for subject: %s", subject_text)
        filter_criteria = f'@SQL="urn:schemas:httpmail:subject" LIKE \'%{subject_text}%\''
        emit_progress(
            progress,
            "info",
            "Applying Outlook subject filter.",
            {"subject": subject_text, "filter": filter_criteria},
        )
        try:
            messages = self.inbox.Items.Restrict(filter_criteria)
        except Exception as exc:
            logging.exception("Outlook Restrict failed for subject %s: %s", subject_text, exc)
            emit_progress(progress, "error", "Outlook subject search failed.", {"subject": subject_text, "error": str(exc)})
            raise RuntimeError(f"Outlook subject search failed for '{subject_text}': {exc}") from exc

        message_count: int | None = None
        try:
            message_count = int(messages.Count)
            emit_progress(progress, "info", "Outlook subject search returned messages.", {"count": message_count})
        except Exception as exc:
            logging.warning("Could not read Outlook message count for subject %s: %s", subject_text, exc)
            emit_progress(progress, "warning", "Could not read Outlook message count.", {"error": str(exc)})

        saved_files: list[Path] = []
        found_messages = 0
        emit_progress(progress, "info", "Beginning Outlook message iteration.", {"subject": subject_text})
        try:
            for message in messages:
                found_messages += 1
                message_subject = str(getattr(message, "Subject", ""))
                logging.info("Found email: %s", message_subject)
                emit_progress(
                    progress,
                    "info",
                    "Found matching Outlook email.",
                    {"index": found_messages, "subject": message_subject},
                )

                attachments = getattr(message, "Attachments", None)
                attachment_count: int | None = None
                try:
                    attachment_count = int(attachments.Count) if attachments is not None else 0
                    emit_progress(progress, "info", "Email attachment count read.", {"count": attachment_count})
                except Exception as exc:
                    logging.warning("Could not read attachment count for %s: %s", message_subject, exc)
                    emit_progress(progress, "warning", "Could not read email attachment count.", {"error": str(exc)})

                for attachment in attachments:
                    try:
                        save_path = self.attachment_folder_path / attachment.FileName
                        emit_progress(progress, "info", "Saving Outlook attachment.", {"path": str(save_path)})
                        attachment.SaveAsFile(str(save_path))
                        saved_files.append(save_path)
                        logging.info("Saved attachment: %s", save_path)
                        emit_progress(progress, "success", "Saved Outlook attachment.", {"path": str(save_path)})
                    except Exception as exc:
                        logging.exception("Failed saving Outlook attachment for subject %s: %s", message_subject, exc)
                        emit_progress(progress, "error", "Failed saving Outlook attachment.", {"error": str(exc)})
                        raise RuntimeError(f"Failed saving Outlook attachment for '{message_subject}': {exc}") from exc
        except Exception:
            raise

        if not saved_files:
            if message_count == 0 or found_messages == 0:
                message = f"No Outlook email found for subject: {subject_text}"
            else:
                message = f"Matching Outlook email found, but no attachments were saved for subject: {subject_text}"
            logging.warning(message)
            emit_progress(
                progress,
                "error",
                "No payment report attachments found.",
                {"subject": subject_text, "message_count": message_count, "iterated_messages": found_messages},
            )
            raise FileNotFoundError(message)

        return saved_files


def _emit_helper_progress(level: str, message: str, details: dict[str, Any] | None = None) -> None:
    print(
        json.dumps(
            {
                "type": "event",
                "level": level,
                "message": message,
                "details": details or {},
            }
        ),
        flush=True,
    )


def _parse_helper_stdout(stdout: str, progress: ProgressCallback | None = None) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                emit_progress(progress, "info", "Outlook helper output.", {"line": line})
            continue

        payload_type = payload.get("type")
        if payload_type == "event":
            emit_progress(
                progress,
                str(payload.get("level") or "info"),
                str(payload.get("message") or ""),
                payload.get("details") if isinstance(payload.get("details"), dict) else {},
            )
        elif payload_type == "result":
            result = payload

    return result


def _run_outlook_helper(args: list[str], progress: ProgressCallback | None = None) -> dict[str, Any]:
    command = [sys.executable, str(Path(__file__).resolve()), *args]
    emit_progress(
        progress,
        "info",
        "Starting isolated Outlook helper process.",
        {"timeout_seconds": OUTLOOK_HELPER_TIMEOUT_SECONDS},
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=OUTLOOK_HELPER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

        _parse_helper_stdout(stdout, progress)
        emit_progress(
            progress,
            "error",
            "Outlook helper timed out before completing.",
            {"timeout_seconds": OUTLOOK_HELPER_TIMEOUT_SECONDS, "stderr": stderr.strip()},
        )
        raise TimeoutError(
            "Outlook did not respond within "
            f"{OUTLOOK_HELPER_TIMEOUT_SECONDS} seconds. Open Classic Outlook in this Windows session, "
            "dismiss any profile/login/security prompts, then try again."
        ) from exc

    result = _parse_helper_stdout(completed.stdout or "", progress)
    if completed.stderr:
        emit_progress(progress, "warning", "Outlook helper stderr output.", {"stderr": completed.stderr.strip()})

    if completed.returncode != 0:
        message = "Outlook helper failed."
        if result and result.get("error"):
            message = str(result["error"])
        raise RuntimeError(message)

    if result is None:
        raise RuntimeError("Outlook helper finished without returning a result.")

    return result


def download_outlook_attachments_with_timeout(
    report_date: datetime,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    result = _run_outlook_helper(
        ["--download-outlook-attachments", report_date.strftime("%Y-%m-%d")],
        progress=progress,
    )
    attachments = [Path(path) for path in result.get("attachments", []) if path]
    if not attachments:
        raise FileNotFoundError("Outlook helper completed but did not return saved attachments.")
    return attachments


def test_outlook_connection_with_timeout(progress: ProgressCallback | None = None) -> dict[str, Any]:
    result = _run_outlook_helper(["--test-outlook-connection"], progress=progress)
    return {
        "ok": True,
        "mode": "outlook_connection_test",
        "base_dir": str(BASE_DIR),
        "attachments": [],
        "requested_count": 1,
        "processed_count": 1,
        "skipped_count": 0,
        "skipped_transactions": [],
        "database_mode": DATABASE_MODE,
        "clean_json_dir": str(NEW_JSON_DIR),
        "log_path": str(LOG_DIR / "sla_payment.log"),
        "results": [
            {
                "Status": "processed",
                "Message": str(result.get("message") or "Outlook COM connection is available."),
                "Database Mode": DATABASE_MODE,
            }
        ],
    }


class ExcelReader:
    def __init__(self, folder_path: Path):
        self.folder_path = Path(folder_path)
        self.transactions: list[str] = []

    def read_reports(self) -> list[str]:
        modules = import_required_modules("pandas")
        pd = modules["pandas"]

        for file_path in self.folder_path.glob("*.xlsx"):
            logging.info("Reading Excel file: %s", file_path)
            df = pd.read_excel(file_path, skiprows=4)
            if "Transaction ID" not in df.columns:
                logging.warning("No Transaction ID column found in %s", file_path)
                continue

            for value in df["Transaction ID"].tolist():
                if pd.isna(value):
                    break
                self.transactions.append(str(value).strip())

        self.transactions = list(dict.fromkeys(self.transactions))
        return self.transactions


def get_sql_connection() -> Any:
    modules = import_required_modules("pyodbc")
    pyodbc = modules["pyodbc"]
    return pyodbc.connect(
        "Driver={SQL Server};"
        r"Server=EDS0085PW5SQLV\P17SO50364,50364;"
        "Database=Prod_SharedServices;"
        "Trusted_Connection=yes;"
    )


def get_oracle_connection() -> Any:
    modules = import_required_modules("oracledb", "db_config")
    oracledb = modules["oracledb"]
    db_config = modules["db_config"]
    return oracledb.connect(
        user=db_config.username,
        password=db_config.password,
        dsn=db_config.dsn,
    )


def write_reprocess_log(processed_transactions: list[SLAPayment]) -> Path:
    ensure_payment_dirs()
    log_path = LOG_DIR / "reprocessed_transactions.jsonl"
    with log_path.open("a", encoding="utf-8") as file:
        for item in processed_transactions:
            file.write(json.dumps(item.to_log_dict()) + "\n")
    return log_path


class SLAPaymentAutomationRunner:
    def run_from_email_date(self, report_date: datetime, progress: ProgressCallback | None = None) -> dict[str, Any]:
        configure_payment_logging()
        logging.info("Starting SLA Payment Automation from email date %s", report_date.strftime("%m/%d/%Y"))
        emit_progress(progress, "info", "Starting email date automation.", {"report_date": report_date.strftime("%m/%d/%Y")})

        subject_text = build_report_subject(report_date)
        emit_progress(progress, "info", "Searching Outlook for SLA payment report email.", {"subject": subject_text})
        emit_progress(progress, "info", "Downloading Outlook attachments through timeout-protected helper.")
        attachments = download_outlook_attachments_with_timeout(report_date, progress=progress)
        emit_progress(progress, "info", "Downloaded Outlook attachments.", {"count": len(attachments)})

        emit_progress(progress, "info", "Reading XLSX reports for Transaction ID values.")
        excel_reader = ExcelReader(ATTACHMENTS_DIR)
        transactions = excel_reader.read_reports()
        emit_progress(progress, "info", "Collected unique transaction IDs.", {"count": len(transactions)})

        processed_transactions, skipped_transactions = self._process_transactions(transactions, progress)
        log_path = write_reprocess_log(processed_transactions)
        logging.info("SLA Payment Automation Complete")
        emit_progress(progress, "success", "Email date automation complete.", {"log_path": str(log_path)})
        return self._build_result(
            mode="email_date",
            source={"report_date": report_date.strftime("%m/%d/%Y")},
            attachments=attachments,
            requested_items=transactions,
            processed_transactions=processed_transactions,
            skipped_transactions=skipped_transactions,
            log_path=log_path,
        )

    def test_outlook_connection(self, progress: ProgressCallback | None = None) -> dict[str, Any]:
        configure_payment_logging()
        logging.info("Starting SLA Payment Outlook connection test")
        emit_progress(progress, "info", "Starting Outlook connection test.")
        result = test_outlook_connection_with_timeout(progress=progress)
        emit_progress(progress, "success", "Outlook connection test complete.")
        return result

    def run_from_existing_attachments(self, progress: ProgressCallback | None = None) -> dict[str, Any]:
        configure_payment_logging()
        emit_progress(progress, "info", "Starting automation from existing app-local XLSX attachments.")
        attachments = sorted(ATTACHMENTS_DIR.glob("*.xlsx"))
        emit_progress(progress, "info", "Found existing XLSX attachments.", {"count": len(attachments)})
        if not attachments:
            raise FileNotFoundError(f"No XLSX files found in {ATTACHMENTS_DIR}")

        emit_progress(progress, "info", "Reading XLSX reports for Transaction ID values.")
        excel_reader = ExcelReader(ATTACHMENTS_DIR)
        transactions = excel_reader.read_reports()
        emit_progress(progress, "info", "Collected unique transaction IDs.", {"count": len(transactions)})
        processed_transactions, skipped_transactions = self._process_transactions(transactions, progress)

        log_path = write_reprocess_log(processed_transactions)
        emit_progress(progress, "success", "Existing attachment automation complete.", {"log_path": str(log_path)})
        return self._build_result(
            mode="existing_attachments",
            source={"attachments_dir": str(ATTACHMENTS_DIR)},
            attachments=attachments,
            requested_items=transactions,
            processed_transactions=processed_transactions,
            skipped_transactions=skipped_transactions,
            log_path=log_path,
        )

    def run_from_app_ids(self, app_ids: list[str], progress: ProgressCallback | None = None) -> dict[str, Any]:
        configure_payment_logging()
        logging.info("Starting SLA Payment Automation from app ids: %s", ", ".join(app_ids))
        emit_progress(progress, "info", "Starting App ID automation.", {"count": len(app_ids)})

        processed_transactions: list[SLAPayment] = []
        emit_progress(progress, "info", "Opening SQL Server and Oracle read-only connections.")
        with get_sql_connection() as sql_conn, get_oracle_connection() as oracle_conn:
            for index, app_id in enumerate(app_ids, start=1):
                emit_progress(
                    progress,
                    "info",
                    "Processing App ID.",
                    {"index": index, "total": len(app_ids), "app_id": app_id},
                )
                payment = SLAPayment(old_app_number=app_id, sql_conn=sql_conn, oracle_conn=oracle_conn)
                try:
                    payment.process_from_app_id()
                    logging.info(
                        "Processed App ID: %s | Transaction: %s | App: %s",
                        app_id,
                        payment.transaction_id,
                        payment.app_number,
                    )
                    emit_progress(
                        progress,
                        "success",
                        "App ID processed and clean JSON written.",
                        {
                            "app_id": app_id,
                            "transaction_id": payment.transaction_id,
                            "clean_json_path": str(payment.clean_json_path) if payment.clean_json_path else "",
                        },
                    )
                except Exception as exc:
                    payment.status = "failed"
                    payment.message = str(exc)
                    logging.exception("Failed processing app id %s: %s", app_id, exc)
                    emit_progress(progress, "error", "App ID failed.", {"app_id": app_id, "error": str(exc)})
                processed_transactions.append(payment)

        log_path = write_reprocess_log(processed_transactions)
        logging.info("SLA Payment Automation App ID run complete")
        emit_progress(progress, "success", "App ID automation complete.", {"log_path": str(log_path)})
        return self._build_result(
            mode="app_ids",
            source={"app_ids": app_ids},
            attachments=[],
            requested_items=app_ids,
            processed_transactions=processed_transactions,
            skipped_transactions=[],
            log_path=log_path,
        )

    def _process_transactions(
        self,
        transactions: list[str],
        progress: ProgressCallback | None = None,
    ) -> tuple[list[SLAPayment], list[str]]:
        processed_transactions: list[SLAPayment] = []
        skipped_transactions: list[str] = []
        emit_progress(progress, "info", "Opening SQL Server and Oracle read-only connections.")
        with get_sql_connection() as sql_conn, get_oracle_connection() as oracle_conn:
            for index, transaction_id in enumerate(transactions, start=1):
                emit_progress(
                    progress,
                    "info",
                    "Processing transaction.",
                    {"index": index, "total": len(transactions), "transaction_id": transaction_id},
                )
                if transaction_id in BAD_TRANSACTIONS:
                    skipped_transactions.append(transaction_id)
                    logging.warning("Skipping bad transaction: %s", transaction_id)
                    emit_progress(progress, "warning", "Skipped known bad transaction.", {"transaction_id": transaction_id})
                    continue

                payment = SLAPayment(transaction_id=transaction_id, sql_conn=sql_conn, oracle_conn=oracle_conn)
                try:
                    payment.process_from_transaction()
                    logging.info(
                        "Processed Transaction: %s | Old App: %s | App: %s",
                        transaction_id,
                        payment.old_app_number,
                        payment.app_number,
                    )
                    emit_progress(
                        progress,
                        "success",
                        "Transaction processed and clean JSON written.",
                        {
                            "transaction_id": transaction_id,
                            "old_app_number": payment.old_app_number,
                            "clean_json_path": str(payment.clean_json_path) if payment.clean_json_path else "",
                        },
                    )
                except Exception as exc:
                    payment.status = "failed"
                    payment.message = str(exc)
                    logging.exception("Failed processing transaction %s: %s", transaction_id, exc)
                    emit_progress(progress, "error", "Transaction failed.", {"transaction_id": transaction_id, "error": str(exc)})
                processed_transactions.append(payment)

        return processed_transactions, skipped_transactions

    def _build_result(
        self,
        *,
        mode: str,
        source: dict[str, Any],
        attachments: list[Path],
        requested_items: list[str],
        processed_transactions: list[SLAPayment],
        skipped_transactions: list[str],
        log_path: Path,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": mode,
            "source": source,
            "base_dir": str(BASE_DIR),
            "database_mode": DATABASE_MODE,
            "clean_json_dir": str(NEW_JSON_DIR),
            "attachments": [str(path) for path in attachments],
            "requested_count": len(requested_items),
            "processed_count": len(processed_transactions),
            "skipped_count": len(skipped_transactions),
            "skipped_transactions": skipped_transactions,
            "log_path": str(log_path),
            "results": [payment.to_log_dict() | {"Message": payment.message} for payment in processed_transactions],
        }


def _run_helper_cli() -> int:
    ensure_payment_dirs()
    configure_payment_logging()
    pythoncom = None
    try:
        pythoncom = import_module("pythoncom")
        pythoncom.CoInitialize()
        _emit_helper_progress("info", "Python COM initialized in Outlook helper process.")
    except ImportError:
        pythoncom = None

    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "--test-outlook-connection":
            try:
                OutlookEmailReader(ATTACHMENTS_DIR, progress=_emit_helper_progress)
                print(
                    json.dumps(
                        {
                            "type": "result",
                            "ok": True,
                            "message": "Outlook COM object, MAPI namespace, and default inbox opened.",
                        }
                    ),
                    flush=True,
                )
                return 0
            except Exception as exc:
                logging.exception("Outlook connection test failed: %s", exc)
                _emit_helper_progress("error", "Outlook connection test failed.", {"error": str(exc)})
                print(json.dumps({"type": "result", "ok": False, "error": str(exc)}), flush=True)
                return 1

        if len(sys.argv) >= 3 and sys.argv[1] == "--download-outlook-attachments":
            try:
                report_date = parse_report_date(sys.argv[2])
                reader = OutlookEmailReader(ATTACHMENTS_DIR, progress=_emit_helper_progress)
                attachments = reader.retrieve_attachments(report_date, progress=_emit_helper_progress)
                print(
                    json.dumps(
                        {
                            "type": "result",
                            "ok": True,
                            "attachments": [str(path) for path in attachments],
                        }
                    ),
                    flush=True,
                )
                return 0
            except Exception as exc:
                logging.exception("Outlook attachment helper failed: %s", exc)
                _emit_helper_progress("error", "Outlook attachment helper failed.", {"error": str(exc)})
                print(json.dumps({"type": "result", "ok": False, "error": str(exc)}), flush=True)
                return 1

        return 0
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(_run_helper_cli())
