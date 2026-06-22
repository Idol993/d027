import os
import random
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from common.utils import (
    setup_logger,
    get_now_iso,
    get_now_str,
    ensure_dir,
    write_json_file,
    read_json_file,
    hours_between,
    GrayBatchStatus,
    FuseLevel,
    ReleaseStatus
)
from modules.audit import AuditLogger


class GrayReleaseManager:
    def __init__(self, config, audit_logger: AuditLogger = None):
        self.config = config
        self.audit_logger = audit_logger
        self.gray_config = config["gray_release"]
        self.monitor_config = config["monitor"]
        self.fuse_config = config["fuse"]
        self.data_dir = config["storage"]["data_dir"]
        self.gray_dir = os.path.join(self.data_dir, "gray_release")
        self.monitor_dir = os.path.join(self.data_dir, "monitor")
        ensure_dir(self.gray_dir)
        ensure_dir(self.monitor_dir)
        self.logger = setup_logger("gray_release", os.path.join(self.data_dir, "gray_release.log"))

    def plan_gray_batches(self, release_id: str, centers: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.logger.info(f"规划灰度批次: release_id={release_id}, centers={len(centers)}个")

        batches_config = sorted(self.gray_config["batches"], key=lambda x: x["order"])

        non_core_centers = []
        regular_centers = []
        core_centers = []

        for center in centers:
            enrolling = center.get("enrolling_count", 0)
            is_hub = center.get("is_hub", False)

            if enrolling > 50 or is_hub:
                core_centers.append(center)
            elif enrolling >= 10:
                regular_centers.append(center)
            else:
                non_core_centers.append(center)

        batches = []
        batch_levels = [
            ("non_core", non_core_centers, batches_config[0] if len(batches_config) > 0 else {}),
            ("regular", regular_centers, batches_config[1] if len(batches_config) > 1 else {}),
            ("core", core_centers, batches_config[2] if len(batches_config) > 2 else {})
        ]

        batch_no = 1
        for level, level_centers, cfg in batch_levels:
            if level_centers:
                batch = {
                    "batch_no": batch_no,
                    "level": level,
                    "label": cfg.get("label", level),
                    "center_count": len(level_centers),
                    "centers": [c["id"] for c in level_centers],
                    "center_details": level_centers,
                    "status": GrayBatchStatus.PENDING,
                    "observation_hours": cfg.get("observation_hours", 4),
                    "release_time": None,
                    "rollback_time": None,
                    "monitor_metrics": [],
                    "fuse_events": []
                }
                batches.append(batch)
                batch_no += 1

        gray_plan = {
            "release_id": release_id,
            "total_centers": len(centers),
            "batch_count": len(batches),
            "batches": batches,
            "created_at": get_now_iso(),
            "current_batch": 0,
            "status": "PLANNED"
        }

        self._save_gray_plan(gray_plan)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="GRAY_PLAN_CREATED",
                operator="system",
                target_type="release",
                target_id=release_id,
                after_value={
                    "batch_count": len(batches),
                    "total_centers": len(centers),
                    "batches": [{"level": b["level"], "count": b["center_count"]} for b in batches]
                },
                remark=f"灰度发布计划创建完成，共{len(batches)}批次，{len(centers)}个中心"
            )

        return gray_plan

    def start_next_batch(self, release_id: str, operator: str = "system") -> Dict[str, Any]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        current_idx = gray_plan["current_batch"]

        if current_idx >= len(gray_plan["batches"]):
            return {"status": "ALL_COMPLETED", "message": "所有批次均已发布"}

        batch = gray_plan["batches"][current_idx]

        if batch["status"] == GrayBatchStatus.RELEASING or batch["status"] == GrayBatchStatus.OBSERVING:
            return {"status": "IN_PROGRESS", "message": f"当前批次 {batch['batch_no']} 正在进行中"}

        batch["status"] = GrayBatchStatus.RELEASING
        batch["release_time"] = get_now_iso()
        gray_plan["status"] = "RELEASING"
        gray_plan["current_batch"] = current_idx

        self._save_gray_plan(gray_plan)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="GRAY_BATCH_STARTED",
                operator=operator,
                target_type="release",
                target_id=release_id,
                after_value={
                    "batch_no": batch["batch_no"],
                    "level": batch["level"],
                    "center_count": batch["center_count"]
                },
                remark=f"第{batch['batch_no']}批次灰度发布开始，{batch['center_count']}个中心"
            )

        self.logger.info(f"第{batch['batch_no']}批次发布开始: {batch['center_count']}个中心")

        return {
            "status": "STARTED",
            "batch_no": batch["batch_no"],
            "level": batch["level"],
            "center_count": batch["center_count"],
            "centers": batch["centers"]
        }

    def complete_batch_release(self, release_id: str,
                               success: bool = True,
                               operator: str = "system") -> Dict[str, Any]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        current_idx = gray_plan["current_batch"]
        batch = gray_plan["batches"][current_idx]

        if not success:
            batch["status"] = GrayBatchStatus.ROLLED_BACK
            gray_plan["status"] = "FAILED"
            self._save_gray_plan(gray_plan)
            return {"status": "FAILED", "batch_no": batch["batch_no"]}

        batch["status"] = GrayBatchStatus.OBSERVING
        batch["observation_start_time"] = get_now_iso()
        gray_plan["status"] = "OBSERVING"

        self._save_gray_plan(gray_plan)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="GRAY_BATCH_RELEASE_COMPLETED",
                operator=operator,
                target_type="release",
                target_id=release_id,
                after_value={"batch_no": batch["batch_no"], "status": "OBSERVING"},
                remark=f"第{batch['batch_no']}批次发布完成，进入观察期"
            )

        return {
            "status": "OBSERVING",
            "batch_no": batch["batch_no"],
            "observation_hours": batch["observation_hours"]
        }

    def check_observation_complete(self, release_id: str) -> Dict[str, Any]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        current_idx = gray_plan["current_batch"]
        if current_idx >= len(gray_plan["batches"]):
            return {"complete": True, "all_done": True}

        batch = gray_plan["batches"][current_idx]
        if batch["status"] != GrayBatchStatus.OBSERVING:
            return {"complete": False, "reason": "not_observing"}

        if not batch.get("observation_start_time"):
            return {"complete": False, "reason": "no_start_time"}

        start_time = datetime.fromisoformat(batch["observation_start_time"])
        now = datetime.now()
        elapsed_hours = (now - start_time).total_seconds() / 3600
        remaining_hours = max(0, batch["observation_hours"] - elapsed_hours)

        if elapsed_hours >= batch["observation_hours"]:
            batch["status"] = GrayBatchStatus.COMPLETED
            gray_plan["current_batch"] = current_idx + 1

            if gray_plan["current_batch"] >= len(gray_plan["batches"]):
                gray_plan["status"] = "ALL_COMPLETED"

            self._save_gray_plan(gray_plan)

            if self.audit_logger:
                self.audit_logger.log(
                    operation_type="GRAY_BATCH_OBSERVATION_COMPLETED",
                    operator="system",
                    target_type="release",
                    target_id=release_id,
                    after_value={"batch_no": batch["batch_no"], "status": "COMPLETED"},
                    remark=f"第{batch['batch_no']}批次观察期结束"
                )

            return {
                "complete": True,
                "batch_no": batch["batch_no"],
                "all_done": gray_plan["current_batch"] >= len(gray_plan["batches"])
            }

        return {
            "complete": False,
            "batch_no": batch["batch_no"],
            "elapsed_hours": round(elapsed_hours, 2),
            "remaining_hours": round(remaining_hours, 2),
            "total_hours": batch["observation_hours"]
        }

    def collect_metrics(self, release_id: str, mock_data: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        current_idx = gray_plan["current_batch"]
        if current_idx >= len(gray_plan["batches"]):
            return []

        batch = gray_plan["batches"][current_idx]
        metrics_config = self.monitor_config["metrics"]

        collected = []
        for metric_cfg in metrics_config:
            metric_name = metric_cfg["name"]

            if mock_data and metric_name in mock_data:
                metric_value = mock_data[metric_name]
            else:
                metric_value = self._simulate_metric_value(metric_cfg, batch)

            metric_record = {
                "metric_name": metric_name,
                "metric_label": metric_cfg["label"],
                "metric_value": metric_value,
                "unit": metric_cfg.get("unit", ""),
                "warn_threshold": metric_cfg["warn_threshold"],
                "fuse_threshold": metric_cfg["fuse_threshold"],
                "is_reverse": metric_cfg.get("reverse", False),
                "collected_at": get_now_iso(),
                "batch_no": batch["batch_no"]
            }

            metric_record["status"] = self._evaluate_metric_status(metric_record)
            collected.append(metric_record)
            batch["monitor_metrics"].append(metric_record)

        self._save_gray_plan(gray_plan)
        self._save_metric_history(release_id, batch["batch_no"], collected)

        return collected

    def _simulate_metric_value(self, metric_cfg: Dict[str, Any], batch: Dict[str, Any]) -> float:
        base_values = {
            "data_anomaly_rate": 0.8,
            "entry_delay_rate": 5.0,
            "approval_block_rate": 8.0,
            "system_error_rate": 0.3,
            "login_success_rate": 99.5
        }
        base = base_values.get(metric_cfg["name"], 5.0)

        if metric_cfg.get("reverse"):
            variation = random.uniform(-1.0, 0.5)
            value = base + variation
            return round(max(0, min(100, value)), 2)
        else:
            variation = random.uniform(-0.5, 0.5) * base
            return round(max(0, base + variation), 2)

    def _evaluate_metric_status(self, metric: Dict[str, Any]) -> str:
        value = metric["metric_value"]
        is_reverse = metric["is_reverse"]

        if is_reverse:
            if value >= metric["warn_threshold"]:
                return "NORMAL"
            elif value >= metric["fuse_threshold"]:
                return "WARN"
            else:
                return "FUSE"
        else:
            if value <= metric["warn_threshold"]:
                return "NORMAL"
            elif value <= metric["fuse_threshold"]:
                return "WARN"
            else:
                return "FUSE"

    def check_fuse_condition(self, release_id: str) -> Dict[str, Any]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        current_idx = gray_plan["current_batch"]
        if current_idx >= len(gray_plan["batches"]):
            return {"triggered": False, "reason": "no_active_batch"}

        batch = gray_plan["batches"][current_idx]
        metrics = batch["monitor_metrics"]

        if not metrics:
            return {"triggered": False, "reason": "no_metrics"}

        recent_metrics = self._get_recent_metrics_by_name(metrics)

        fuse_level = 0
        trigger_reasons = []
        warn_count = 0
        fuse_count = 0

        continuous_cycles = self.fuse_config["continuous_cycles_for_fuse"]

        for metric_name, metric_list in recent_metrics.items():
            if not metric_list:
                continue

            latest = metric_list[-1]
            status = latest["status"]

            if status == "WARN":
                warn_count += 1
                trigger_reasons.append({
                    "metric": latest["metric_label"],
                    "value": latest["metric_value"],
                    "threshold": latest["warn_threshold"],
                    "level": "warn"
                })
            elif status == "FUSE":
                recent_fuse_count = sum(1 for m in metric_list[-continuous_cycles:]
                                         if m["status"] == "FUSE")
                if recent_fuse_count >= continuous_cycles:
                    fuse_count += 1
                    trigger_reasons.append({
                        "metric": latest["metric_label"],
                        "value": latest["metric_value"],
                        "threshold": latest["fuse_threshold"],
                        "continuous_cycles": recent_fuse_count,
                        "level": "fuse"
                    })

        if fuse_count >= 2:
            fuse_level = FuseLevel.FULL_ROLLBACK
        elif fuse_count >= 1:
            fuse_level = FuseLevel.PARTIAL_ROLLBACK
        elif warn_count >= 1:
            fuse_level = FuseLevel.WARN

        result = {
            "triggered": fuse_level > 0,
            "fuse_level": fuse_level,
            "fuse_level_name": self._get_fuse_level_name(fuse_level),
            "trigger_reasons": trigger_reasons,
            "warn_count": warn_count,
            "fuse_count": fuse_count,
            "batch_no": batch["batch_no"]
        }

        if fuse_level > 0:
            fuse_event = {
                "fuse_level": fuse_level,
                "fuse_level_name": self._get_fuse_level_name(fuse_level),
                "trigger_time": get_now_iso(),
                "reasons": trigger_reasons,
                "metrics_snapshot": recent_metrics
            }
            batch["fuse_events"].append(fuse_event)
            self._save_gray_plan(gray_plan)

            if self.audit_logger:
                self.audit_logger.log(
                    operation_type="FUSE_TRIGGERED",
                    operator="system",
                    target_type="release",
                    target_id=release_id,
                    after_value={
                        "fuse_level": fuse_level,
                        "level_name": self._get_fuse_level_name(fuse_level),
                        "batch_no": batch["batch_no"],
                        "reasons": trigger_reasons
                    },
                    remark=f"熔断触发: {self._get_fuse_level_name(fuse_level)}"
                )

        return result

    def _get_recent_metrics_by_name(self, metrics: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        by_name = {}
        for m in metrics:
            name = m["metric_name"]
            if name not in by_name:
                by_name[name] = []
            by_name[name].append(m)

        for name in by_name:
            by_name[name].sort(key=lambda x: x["collected_at"])

        return by_name

    def _get_fuse_level_name(self, level: int) -> str:
        names = {
            FuseLevel.WARN: "一级熔断（预警）",
            FuseLevel.PARTIAL_ROLLBACK: "二级熔断（部分回滚）",
            FuseLevel.FULL_ROLLBACK: "三级熔断（全量回滚）"
        }
        return names.get(level, f"未知({level})")

    def execute_rollback(self, release_id: str, rollback_version: str,
                         scope: str = "current",
                         operator: str = "system") -> Dict[str, Any]:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            raise ValueError(f"灰度计划不存在: {release_id}")

        self.logger.info(f"执行回滚: release_id={release_id}, scope={scope}, version={rollback_version}")

        if scope == "all":
            batches_to_rollback = list(reversed(gray_plan["batches"]))
            gray_plan["status"] = "FULL_ROLLBACK"
        else:
            current_idx = gray_plan["current_batch"]
            if current_idx >= len(gray_plan["batches"]):
                current_idx = len(gray_plan["batches"]) - 1
            batches_to_rollback = [gray_plan["batches"][current_idx]]
            gray_plan["status"] = "PARTIAL_ROLLBACK"

        rolled_back = []
        for batch in batches_to_rollback:
            if batch["status"] in [GrayBatchStatus.OBSERVING, GrayBatchStatus.RELEASING, GrayBatchStatus.COMPLETED]:
                batch["status"] = GrayBatchStatus.ROLLED_BACK
                batch["rollback_time"] = get_now_iso()
                batch["rollback_version"] = rollback_version
                rolled_back.append(batch["batch_no"])

        self._save_gray_plan(gray_plan)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="ROLLBACK_EXECUTED",
                operator=operator,
                target_type="release",
                target_id=release_id,
                after_value={
                    "scope": scope,
                    "rollback_version": rollback_version,
                    "rolled_back_batches": rolled_back
                },
                remark=f"回滚执行: {scope}，版本 {rollback_version}"
            )

        return {
            "status": "COMPLETED",
            "scope": scope,
            "rollback_version": rollback_version,
            "rolled_back_batches": rolled_back,
            "rollback_time": get_now_iso()
        }

    def get_gray_plan(self, release_id: str) -> Optional[Dict[str, Any]]:
        return self._load_gray_plan(release_id)

    def _load_gray_plan(self, release_id: str) -> Optional[Dict[str, Any]]:
        file_path = os.path.join(self.gray_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None

    def _save_gray_plan(self, plan: Dict[str, Any]):
        file_path = os.path.join(self.gray_dir, f"{plan['release_id']}.json")
        write_json_file(file_path, plan)

    def _save_metric_history(self, release_id: str, batch_no: int, metrics: List[Dict[str, Any]]):
        date_str = datetime.now().strftime("%Y%m%d")
        file_path = os.path.join(self.monitor_dir, f"{release_id}_batch{batch_no}_{date_str}.json")

        history = []
        if os.path.exists(file_path):
            history = read_json_file(file_path)

        history.extend(metrics)
        write_json_file(file_path, history)

    def generate_gray_report(self, release_id: str) -> str:
        gray_plan = self._load_gray_plan(release_id)
        if not gray_plan:
            return "灰度计划不存在"

        lines = []
        lines.append("=" * 60)
        lines.append("  灰度发布报告")
        lines.append("=" * 60)
        lines.append(f"发布编号: {gray_plan['release_id']}")
        lines.append(f"中心总数: {gray_plan['total_centers']}")
        lines.append(f"批 次 数: {gray_plan['batch_count']}")
        lines.append(f"当前状态: {gray_plan['status']}")
        lines.append(f"创建时间: {gray_plan['created_at']}")
        lines.append("")

        lines.append("【批次详情】")
        for i, batch in enumerate(gray_plan["batches"], 1):
            status_map = {
                GrayBatchStatus.PENDING: "待发布",
                GrayBatchStatus.RELEASING: "发布中",
                GrayBatchStatus.OBSERVING: "观察中",
                GrayBatchStatus.COMPLETED: "已完成",
                GrayBatchStatus.ROLLED_BACK: "已回滚"
            }
            status_str = status_map.get(batch["status"], batch["status"])

            lines.append(f"  第{i}批 - {batch['label']}")
            lines.append(f"    中心数量: {batch['center_count']} 个")
            lines.append(f"    状态: {status_str}")
            lines.append(f"    观察期: {batch['observation_hours']} 小时")
            if batch.get("release_time"):
                lines.append(f"    发布时间: {batch['release_time']}")
            if batch.get("rollback_time"):
                lines.append(f"    回滚时间: {batch['rollback_time']}")
            if batch.get("fuse_events"):
                lines.append(f"    熔断事件: {len(batch['fuse_events'])} 次")
                for fe in batch["fuse_events"]:
                    lines.append(f"      - {fe['fuse_level_name']} @ {fe['trigger_time']}")
            lines.append("")

        return "\n".join(lines)
