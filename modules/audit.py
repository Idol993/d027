import os
import json
from datetime import datetime
from pathlib import Path

from common.utils import (
    generate_trace_id,
    get_now_iso,
    ensure_dir,
    calc_hash_chain,
    setup_logger,
    write_json_file,
    read_json_file,
    json_dumps
)


class AuditLogger:
    def __init__(self, config):
        self.config = config
        self.audit_dir = config["storage"]["audit_dir"]
        self.use_hash_chain = config["audit"]["hash_chain"]
        ensure_dir(self.audit_dir)
        self.logger = setup_logger("audit", os.path.join(self.audit_dir, "audit.log"))
        self._chain_file = os.path.join(self.audit_dir, "hash_chain.json")
        self._last_hash = self._load_last_hash()

    def _load_last_hash(self):
        if os.path.exists(self._chain_file):
            data = read_json_file(self._chain_file)
            return data.get("last_hash", "0" * 64)
        return "0" * 64

    def _save_last_hash(self, hash_value):
        write_json_file(self._chain_file, {
            "last_hash": hash_value,
            "updated_at": get_now_iso()
        })

    def log(self, operation_type, operator, target_type, target_id,
            before_value=None, after_value=None, remark=None, trace_id=None):
        trace_id = trace_id or generate_trace_id()
        now_iso = get_now_iso()

        record = {
            "trace_id": trace_id,
            "operation_type": operation_type,
            "operator": operator,
            "target_type": target_type,
            "target_id": target_id,
            "before_value": before_value,
            "after_value": after_value,
            "remark": remark,
            "created_at": now_iso
        }

        if self.use_hash_chain:
            record["prev_hash"] = self._last_hash
            record_hash = calc_hash_chain(self._last_hash, record)
            record["hash"] = record_hash
            self._last_hash = record_hash
            self._save_last_hash(record_hash)

        self.logger.info(json_dumps(record, indent=0))

        date_str = datetime.now().strftime("%Y%m%d")
        daily_file = os.path.join(self.audit_dir, f"audit_{date_str}.json")
        self._append_daily_record(daily_file, record)

        return trace_id

    def _append_daily_record(self, file_path, record):
        records = []
        if os.path.exists(file_path):
            records = read_json_file(file_path)
        records.append(record)
        write_json_file(file_path, records)

    def query(self, operation_type=None, operator=None, target_type=None,
              target_id=None, start_time=None, end_time=None, trace_id=None):
        results = []
        audit_files = sorted(Path(self.audit_dir).glob("audit_*.json"))

        for f in audit_files:
            try:
                records = read_json_file(str(f))
                for r in records:
                    if trace_id and r.get("trace_id") != trace_id:
                        continue
                    if operation_type and r.get("operation_type") != operation_type:
                        continue
                    if operator and r.get("operator") != operator:
                        continue
                    if target_type and r.get("target_type") != target_type:
                        continue
                    if target_id and r.get("target_id") != target_id:
                        continue
                    if start_time and r.get("created_at") < start_time:
                        continue
                    if end_time and r.get("created_at") > end_time:
                        continue
                    results.append(r)
            except Exception:
                continue

        return sorted(results, key=lambda x: x.get("created_at", ""))

    def verify_integrity(self):
        audit_files = sorted(Path(self.audit_dir).glob("audit_*.json"))
        prev_hash = "0" * 64
        total_records = 0
        errors = []

        for f in audit_files:
            try:
                records = read_json_file(str(f))
                for r in records:
                    total_records += 1
                    if self.use_hash_chain:
                        if r.get("prev_hash") != prev_hash:
                            errors.append({
                                "trace_id": r.get("trace_id"),
                                "error": "prev_hash mismatch",
                                "expected": prev_hash,
                                "actual": r.get("prev_hash")
                            })
                        record_for_hash = {k: v for k, v in r.items() if k not in ["hash"]}
                        expected_hash = calc_hash_chain(prev_hash, record_for_hash)
                        if r.get("hash") != expected_hash:
                            errors.append({
                                "trace_id": r.get("trace_id"),
                                "error": "hash mismatch",
                                "expected": expected_hash,
                                "actual": r.get("hash")
                            })
                        prev_hash = r.get("hash", prev_hash)
            except Exception as e:
                errors.append({"file": str(f), "error": str(e)})

        return {
            "total_records": total_records,
            "error_count": len(errors),
            "errors": errors,
            "integrity_ok": len(errors) == 0
        }
