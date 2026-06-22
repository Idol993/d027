import os
import yaml
import json
import uuid
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path


def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def generate_release_no(prefix="REL"):
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    random_str = uuid.uuid4().hex[:6].upper()
    return f"{prefix}-{date_str}-{random_str}"


def generate_trace_id():
    return uuid.uuid4().hex


def get_now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_now_iso():
    return datetime.now().isoformat()


def parse_datetime(dt_str):
    if isinstance(dt_str, datetime):
        return dt_str
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


def ensure_dir(dir_path):
    Path(dir_path).mkdir(parents=True, exist_ok=True)


def setup_logger(name, log_file=None, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if log_file:
            ensure_dir(os.path.dirname(log_file))
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

    return logger


def json_dumps(obj, indent=2, ensure_ascii=False):
    return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, default=str)


def json_loads(s):
    return json.loads(s)


def read_json_file(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_file(file_path, data):
    ensure_dir(os.path.dirname(file_path))
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def sha256_hash(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def calc_hash_chain(prev_hash, current_data):
    content = f"{prev_hash}|{json_dumps(current_data, indent=0)}"
    return sha256_hash(content)


def days_between(d1, d2):
    d1 = parse_datetime(d1) if isinstance(d1, str) else d1
    d2 = parse_datetime(d2) if isinstance(d2, str) else d2
    return abs((d2 - d1).days)


def hours_between(d1, d2):
    d1 = parse_datetime(d1) if isinstance(d1, str) else d1
    d2 = parse_datetime(d2) if isinstance(d2, str) else d2
    return abs((d2 - d1).total_seconds() / 3600)


class ReleaseStatus:
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    BLOCKED = "BLOCKED"
    APPROVING = "APPROVING"
    REJECTED = "REJECTED"
    RELEASING = "RELEASING"
    GRAYING = "GRAYING"
    FUSED = "FUSED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"


class ValidationResult:
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"


class Severity:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DELEGATED = "DELEGATED"
    POST_APPROVED = "POST_APPROVED"


class ReleaseType:
    NORMAL = "NORMAL"
    HOTFIX = "HOTFIX"


class GrayBatchStatus:
    PENDING = "PENDING"
    RELEASING = "RELEASING"
    OBSERVING = "OBSERVING"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"


class FuseLevel:
    WARN = 1
    PARTIAL_ROLLBACK = 2
    FULL_ROLLBACK = 3
