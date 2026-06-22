#!/usr/bin/env python3
"""
CTMS 系统版本发布与智能回滚自动化平台 — 主入口
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.utils import (
    load_config,
    generate_release_no,
    setup_logger,
    get_now_iso,
    get_now_str,
    ensure_dir,
    write_json_file,
    read_json_file,
    ReleaseStatus,
    ReleaseType
)
from modules.audit import AuditLogger
from modules.pre_validation import PreValidator
from modules.approval import ApprovalWorkflow
from modules.gray_release import GrayReleaseManager
from modules.report import ReportGenerator


class ReleaseOrchestrator:
    def __init__(self, config_path="config.yaml"):
        self.config = load_config(config_path)
        self.data_dir = self.config["storage"]["data_dir"]
        self.release_dir = self.config["storage"]["release_dir"]
        ensure_dir(self.data_dir)
        ensure_dir(self.release_dir)

        self.logger = setup_logger("orchestrator", os.path.join(self.data_dir, "orchestrator.log"))

        self.audit_logger = AuditLogger(self.config)
        self.validator = PreValidator(self.config, self.audit_logger)
        self.approval_workflow = ApprovalWorkflow(self.config, self.audit_logger)
        self.gray_manager = GrayReleaseManager(self.config, self.audit_logger)
        self.report_generator = ReportGenerator(self.config, self.audit_logger)

    def create_release_request(self, version: str, project_id: str, title: str,
                                description: str, applicant: str,
                                is_hotfix: bool = False,
                                hotfix_reason: str = None,
                                custom_approvers: Dict[str, str] = None) -> Dict[str, Any]:
        release_id = generate_release_no(self.config["release"]["release_no_prefix"])

        self.logger.info(f"创建发布申请: {release_id}, version={version}, project={project_id}")

        release = {
            "release_id": release_id,
            "version": version,
            "project_id": project_id,
            "title": title,
            "description": description,
            "applicant": applicant,
            "release_type": ReleaseType.HOTFIX if is_hotfix else ReleaseType.NORMAL,
            "status": ReleaseStatus.DRAFT,
            "is_hotfix": is_hotfix,
            "hotfix_reason": hotfix_reason,
            "created_at": get_now_iso(),
            "updated_at": get_now_iso(),
            "current_stage": "draft",
            "validation_result": None,
            "approval_flow": None,
            "gray_plan": None,
            "rollback_info": None
        }

        self._save_release(release)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="RELEASE_CREATED",
                operator=applicant,
                target_type="release",
                target_id=release_id,
                after_value={"version": version, "project_id": project_id, "title": title},
                remark=f"创建发布申请: {title}"
            )

        return release

    def submit_for_validation(self, release_id: str,
                              mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        if release["status"] not in [ReleaseStatus.DRAFT, ReleaseStatus.BLOCKED]:
            raise ValueError(f"当前状态不允许提交校验: {release['status']}")

        self.logger.info(f"提交前置校验: {release_id}")
        release["status"] = ReleaseStatus.VALIDATING
        release["current_stage"] = "validation"
        release["updated_at"] = get_now_iso()
        self._save_release(release)

        validation_result = self.validator.validate_all(
            release_id,
            release["project_id"],
            release["version"],
            mock_data
        )

        release["validation_result"] = validation_result
        release["updated_at"] = get_now_iso()

        if validation_result["summary"]["blocked"]:
            release["status"] = ReleaseStatus.BLOCKED
            release["current_stage"] = "blocked"
            self.logger.warning(f"前置校验阻断: {release_id}")
        else:
            release["status"] = ReleaseStatus.APPROVING
            release["current_stage"] = "approval"
            self._init_approval_flow(release)
            self.logger.info(f"前置校验通过，进入审批: {release_id}")

        self._save_release(release)
        return release

    def _init_approval_flow(self, release: Dict[str, Any]):
        approval_flow = self.approval_workflow.create_approval_flow(
            release_id=release["release_id"],
            version=release["version"],
            project_id=release["project_id"],
            applicant=release["applicant"],
            is_hotfix=release["is_hotfix"],
            hotfix_reason=release.get("hotfix_reason")
        )
        release["approval_flow"] = approval_flow

    def approve_node(self, release_id: str, node_name: str, approver: str,
                     comment: str = None) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        if release["status"] != ReleaseStatus.APPROVING:
            raise ValueError(f"当前状态不允许审批: {release['status']}")

        flow = self.approval_workflow.approve(release_id, node_name, approver, comment)
        release["approval_flow"] = flow
        release["updated_at"] = get_now_iso()

        if flow["status"] == "APPROVED":
            release["status"] = ReleaseStatus.RELEASING
            release["current_stage"] = "gray_release"
            self.logger.info(f"审批全部通过，进入灰度发布: {release_id}")

        self._save_release(release)
        return release

    def reject_release(self, release_id: str, node_name: str, approver: str,
                       reject_reason: str) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        flow = self.approval_workflow.reject(release_id, node_name, approver, reject_reason)
        release["approval_flow"] = flow
        release["status"] = ReleaseStatus.REJECTED
        release["current_stage"] = "rejected"
        release["updated_at"] = get_now_iso()

        self._save_release(release)
        self.logger.info(f"发布被驳回: {release_id}, 节点: {node_name}")
        return release

    def start_gray_release(self, release_id: str, centers: List[Dict[str, Any]],
                           operator: str = "system") -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        if release["status"] not in [ReleaseStatus.RELEASING, ReleaseStatus.GRAYING]:
            raise ValueError(f"当前状态不允许灰度发布: {release['status']}")

        if release.get("gray_plan") is None:
            gray_plan = self.gray_manager.plan_gray_batches(release_id, centers)
            release["gray_plan"] = gray_plan
            release["status"] = ReleaseStatus.GRAYING
            release["current_stage"] = "gray_release"
            release["updated_at"] = get_now_iso()
            self._save_release(release)

        self.gray_manager.start_next_batch(release_id, operator)
        release = self._load_release(release_id)
        release["gray_plan"] = self.gray_manager.get_gray_plan(release_id)
        release["updated_at"] = get_now_iso()
        self._save_release(release)

        return release

    def complete_current_batch(self, release_id: str,
                               success: bool = True,
                               operator: str = "system") -> Dict[str, Any]:
        result = self.gray_manager.complete_batch_release(release_id, success, operator)
        release = self._load_release(release_id)
        release["gray_plan"] = self.gray_manager.get_gray_plan(release_id)

        if not success:
            release["status"] = ReleaseStatus.ROLLED_BACK
            release["current_stage"] = "rolled_back"

        release["updated_at"] = get_now_iso()
        self._save_release(release)
        return release

    def monitor_and_check(self, release_id: str,
                          mock_metrics: Dict[str, Any] = None) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        metrics = self.gray_manager.collect_metrics(release_id, mock_metrics)

        fuse_result = self.gray_manager.check_fuse_condition(release_id)

        if fuse_result["triggered"]:
            self.logger.warning(
                f"熔断触发: {release_id}, 级别: {fuse_result['fuse_level_name']}")

            if fuse_result["fuse_level"] >= 2:
                scope = "all" if fuse_result["fuse_level"] >= 3 else "current"
                rollback_result = self.execute_rollback(release_id, scope)
                release = self._load_release(release_id)
                return {
                    "metrics": metrics,
                    "fuse": fuse_result,
                    "rollback": rollback_result
                }

        obs_result = self.gray_manager.check_observation_complete(release_id)

        if obs_result.get("complete") and not obs_result.get("all_done"):
            self.logger.info(f"观察期结束，自动启动下一批次: {release_id}")
            self.gray_manager.start_next_batch(release_id)

        release = self._load_release(release_id)
        release["gray_plan"] = self.gray_manager.get_gray_plan(release_id)
        release["updated_at"] = get_now_iso()

        if obs_result.get("all_done"):
            release["status"] = ReleaseStatus.COMPLETED
            release["current_stage"] = "completed"
            release["completed_at"] = get_now_iso()
            self.logger.info(f"发布全部完成: {release_id}")

        self._save_release(release)

        return {
            "metrics": metrics,
            "fuse": fuse_result,
            "observation": obs_result
        }

    def execute_rollback(self, release_id: str, scope: str = "all",
                         rollback_version: str = None,
                         operator: str = "system") -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        if rollback_version is None:
            rollback_version = self._get_previous_version(release["version"])

        self.logger.info(f"执行回滚: {release_id}, scope={scope}, version={rollback_version}")

        rollback_result = self.gray_manager.execute_rollback(
            release_id, rollback_version, scope, operator
        )

        release = self._load_release(release_id)
        release["gray_plan"] = self.gray_manager.get_gray_plan(release_id)
        release["status"] = ReleaseStatus.ROLLED_BACK
        release["current_stage"] = "rolled_back"
        release["rollback_info"] = {
            "scope": scope,
            "rollback_version": rollback_version,
            "rollback_time": get_now_iso(),
            "reason": "熔断触发自动回滚" if scope != "manual" else "人工手动回滚",
            "operator": operator
        }
        release["updated_at"] = get_now_iso()
        self._save_release(release)

        return rollback_result

    def _get_previous_version(self, current_version: str) -> str:
        parts = current_version.split(".")
        if len(parts) >= 3:
            try:
                patch = int(parts[2])
                if patch > 0:
                    parts[2] = str(patch - 1)
                    return ".".join(parts)
            except ValueError:
                pass
        return f"{current_version}-prev"

    def generate_report(self, release_id: str) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            raise ValueError(f"发布申请不存在: {release_id}")

        gray_plan = self.gray_manager.get_gray_plan(release_id)
        approval_flow = self.approval_workflow.get_approval_flow(release_id)

        report = self.report_generator.generate_release_report(
            release_id,
            release.get("validation_result"),
            approval_flow if approval_flow else release.get("approval_flow"),
            gray_plan if gray_plan else release.get("gray_plan")
        )
        return report

    def get_release(self, release_id: str) -> Dict[str, Any]:
        release = self._load_release(release_id)
        if not release:
            return None

        gray_plan = self.gray_manager.get_gray_plan(release_id)
        if gray_plan:
            release["gray_plan"] = gray_plan

        approval_flow = self.approval_workflow.get_approval_flow(release_id)
        if approval_flow:
            release["approval_flow"] = approval_flow

        release = self._enrich_release_info(release)

        return release

    def _enrich_release_info(self, release: Dict[str, Any]) -> Dict[str, Any]:
        if not release:
            return release

        release_id = release["release_id"]
        gray_plan = self.gray_manager.get_gray_plan(release_id)
        if gray_plan:
            release["gray_plan"] = gray_plan

            total_cycles = 0
            total_fuse = 0
            for batch in gray_plan.get("batches", []):
                cycles, _, _ = self._calc_monitor_stats(batch)
                total_cycles += cycles
                total_fuse += len(batch.get("fuse_events", []))

            release["_stats"] = {
                "gray_status": gray_plan.get("status", "N/A"),
                "current_batch": gray_plan.get("current_batch", -1) + 1,
                "total_batches": gray_plan.get("batch_count", 0),
                "total_cycles": total_cycles,
                "total_fuse": total_fuse
            }
        else:
            release["_stats"] = {
                "gray_status": "未开始",
                "current_batch": 0,
                "total_batches": 0,
                "total_cycles": 0,
                "total_fuse": 0
            }

        approval_flow = self.approval_workflow.get_approval_flow(release_id)
        if approval_flow:
            release["approval_flow"] = approval_flow
            approved = sum(1 for n in approval_flow.get("nodes", [])
                           if n["status"] in ["APPROVED", "POST_APPROVED"])
            total = len(approval_flow.get("nodes", []))
            release["_stats"]["approval_progress"] = f"{approved}/{total}"
        else:
            release["_stats"]["approval_progress"] = "N/A"

        return release

    def _calc_monitor_stats(self, batch: Dict[str, Any]) -> Tuple[int, int, int]:
        metrics = batch.get("monitor_metrics", [])
        if not metrics:
            return 0, 0, 0

        metric_names = set()
        collection_times = set()
        for m in metrics:
            metric_names.add(m["metric_name"])
            collection_times.add(m["collected_at"])

        n_metrics = len(metric_names)
        n_times = len(collection_times)
        n_records = len(metrics)

        if n_metrics == 0:
            return 0, 0, 0

        if n_times <= 3 or n_times <= n_metrics:
            cycles = n_times
        else:
            cycles = n_records // n_metrics

        return cycles, n_metrics, n_records

    def list_releases(self, status: str = None, project_id: str = None,
                      enrich: bool = True) -> List[Dict[str, Any]]:
        releases = []
        if not os.path.exists(self.release_dir):
            return releases

        for f in os.listdir(self.release_dir):
            if f.endswith(".json"):
                try:
                    release = read_json_file(os.path.join(self.release_dir, f))
                    if status and release.get("status") != status:
                        continue
                    if project_id and release.get("project_id") != project_id:
                        continue
                    if enrich:
                        release = self._enrich_release_info(release)
                    releases.append(release)
                except Exception:
                    continue

        releases.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return releases

    def get_status_summary(self) -> Dict[str, Any]:
        all_releases = self.list_releases()
        status_counts = {}
        for r in all_releases:
            s = r.get("status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "total": len(all_releases),
            "by_status": status_counts
        }

    def _load_release(self, release_id: str) -> Dict[str, Any]:
        file_path = os.path.join(self.release_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None

    def _save_release(self, release: Dict[str, Any]):
        file_path = os.path.join(self.release_dir, f"{release['release_id']}.json")
        write_json_file(file_path, release)

    def get_approval_status_text(self, release_id: str) -> str:
        flow = self.approval_workflow.get_approval_flow(release_id)
        if not flow:
            return f"发布 {release_id} 暂无审批流程"
        return self.approval_workflow.generate_approval_report(release_id)

    def get_gray_status_text(self, release_id: str) -> str:
        gray_plan = self.gray_manager.get_gray_plan(release_id)
        if not gray_plan:
            return f"发布 {release_id} 暂无灰度计划"

        lines = []
        lines.append("=" * 60)
        lines.append("  灰度发布状态")
        lines.append("=" * 60)
        lines.append(f"发布编号: {gray_plan['release_id']}")
        lines.append(f"中心总数: {gray_plan['total_centers']} 个")
        lines.append(f"批 次 数: {gray_plan['batch_count']}")
        lines.append(f"当前状态: {gray_plan['status']}")
        lines.append(f"当前批次: 第 {gray_plan['current_batch'] + 1} 批 / 共 {gray_plan['batch_count']} 批")
        lines.append("")

        lines.append("【批次详情】")
        for i, batch in enumerate(gray_plan["batches"], 1):
            status_map = {
                "PENDING": "待发布",
                "RELEASING": "发布中",
                "OBSERVING": "观察中",
                "COMPLETED": "已完成",
                "ROLLED_BACK": "已回滚"
            }
            status_str = status_map.get(batch["status"], batch["status"])

            prefix = ""
            if batch["status"] == "OBSERVING":
                prefix = "▶ "
            elif batch["status"] == "COMPLETED":
                prefix = "✓ "
            elif batch["status"] == "ROLLED_BACK":
                prefix = "✗ "
            elif batch["status"] == "RELEASING":
                prefix = "→ "
            else:
                prefix = "○ "

            lines.append(f"  {prefix}第{i}批 ({batch['label']})")
            lines.append(f"    中心数量: {batch['center_count']} 个")
            lines.append(f"    状态: {status_str}")
            lines.append(f"    观察期: {batch['observation_hours']} 小时")
            if batch.get("release_time"):
                lines.append(f"    发布时间: {batch['release_time']}")
            if batch.get("rollback_time"):
                lines.append(f"    回滚时间: {batch['rollback_time']}")

            cycles, n_metrics, n_records = self._calc_monitor_stats(batch)
            lines.append(f"    监控轮数: {cycles} 轮 ({n_metrics} 项指标 / 共 {n_records} 条记录)")

            fuse_count = len(batch.get("fuse_events", []))
            if fuse_count > 0:
                lines.append(f"    熔断事件: {fuse_count} 次")
                for fe in batch.get("fuse_events", []):
                    lines.append(f"      - {fe['fuse_level_name']} @ {fe['trigger_time']}")
            lines.append("")

        return "\n".join(lines)

    def get_latest_metrics_text(self, release_id: str) -> str:
        gray_plan = self.gray_manager.get_gray_plan(release_id)
        if not gray_plan:
            return f"发布 {release_id} 暂无灰度计划"

        lines = []
        lines.append("=" * 60)
        lines.append("  最新监控指标")
        lines.append("=" * 60)
        lines.append(f"发布编号: {release_id}")
        lines.append("")

        for batch in gray_plan["batches"]:
            if batch["status"] == "PENDING":
                continue

            lines.append(f"【第{batch['batch_no']}批 - {batch['label']}】")
            metrics = batch.get("monitor_metrics", [])

            if not metrics:
                lines.append("  暂无监控数据")
                lines.append("")
                continue

            latest_by_name = {}
            for m in metrics:
                name = m["metric_name"]
                if name not in latest_by_name or m["collected_at"] > latest_by_name[name]["collected_at"]:
                    latest_by_name[name] = m

            for name, m in sorted(latest_by_name.items()):
                status_icon = "✓" if m["status"] == "NORMAL" else "⚠" if m["status"] == "WARN" else "✗"
                lines.append(f"  {status_icon} {m['metric_label']}: {m['metric_value']}{m.get('unit', '')}")
                lines.append(f"     预警阈值: {m['warn_threshold']}{m.get('unit', '')} | 熔断阈值: {m['fuse_threshold']}{m.get('unit', '')}")
                lines.append(f"     采集时间: {m['collected_at']}")

            lines.append(f"  采集总记录数: {len(metrics)}")
            lines.append("")

        return "\n".join(lines)

    def get_fuse_records_text(self, release_id: str) -> str:
        gray_plan = self.gray_manager.get_gray_plan(release_id)
        if not gray_plan:
            return f"发布 {release_id} 暂无灰度计划"

        lines = []
        lines.append("=" * 60)
        lines.append("  熔断记录")
        lines.append("=" * 60)
        lines.append(f"发布编号: {release_id}")
        lines.append("")

        all_events = []
        for batch in gray_plan["batches"]:
            for event in batch.get("fuse_events", []):
                all_events.append({
                    "batch_no": batch["batch_no"],
                    "batch_label": batch["label"],
                    **event
                })

        if not all_events:
            lines.append("  暂无熔断记录")
            return "\n".join(lines)

        lines.append(f"  熔断总次数: {len(all_events)}")
        lines.append("")

        for i, event in enumerate(all_events, 1):
            lines.append(f"【{i}】第{event['batch_no']}批 - {event['batch_label']}")
            lines.append(f"  熔断级别: {event['fuse_level_name']} (Level {event['fuse_level']})")
            lines.append(f"  触发时间: {event['trigger_time']}")
            lines.append(f"  触发原因:")
            for reason in event.get("reasons", []):
                lines.append(f"    - {reason.get('metric', 'N/A')}: {reason.get('value', 'N/A')}"
                           f" (阈值: {reason.get('threshold', 'N/A')}, 级别: {reason.get('level', 'N/A')})")
            lines.append("")

        return "\n".join(lines)

    def verify_audit_integrity(self, release_id: str) -> Dict[str, Any]:
        release = self.get_release(release_id)
        if not release:
            return {
                "success": False,
                "error": f"发布编号不存在: {release_id}",
                "release_id": release_id
            }

        audit_records = self.audit_logger.query(target_type="release", target_id=release_id)
        if not audit_records:
            return {
                "success": True,
                "release_id": release_id,
                "total_records": 0,
                "note": "暂无审计记录",
                "hash_chain_valid": None,
                "operation_counts": {},
                "gaps": [],
                "mismatches": []
            }

        audit_records.sort(key=lambda x: x.get("created_at", ""))

        operation_counts = {}
        for rec in audit_records:
            op = rec.get("operation_type", "UNKNOWN")
            operation_counts[op] = operation_counts.get(op, 0) + 1

        gaps = []
        mismatches = []
        hash_chain_valid = True

        for i in range(len(audit_records)):
            current = audit_records[i]

            if i == 0:
                pass
            else:
                prev = audit_records[i - 1]
                expected_prev = prev.get("hash", "")
                if current.get("prev_hash") != expected_prev:
                    mismatches.append({
                        "index": i + 1,
                        "trace_id": current.get("trace_id", ""),
                        "operation": current.get("operation_type", ""),
                        "issue": (f"第 {i+1} 条与第 {i} 条 hash 不匹配。"
                                 f"期望: {expected_prev[:16]}..., 实际: {current.get('prev_hash', '缺失')[:16]}...")
                    })
                    hash_chain_valid = False

            current_time = current.get("created_at", "")
            if i > 0:
                prev_time = audit_records[i - 1].get("created_at", "")
                if current_time and prev_time and current_time < prev_time:
                    gaps.append({
                        "index": i + 1,
                        "issue": f"时间顺序异常: 第 {i+1} 条时间({current_time}) 早于 第 {i} 条({prev_time})"
                    })

            if current.get("hash") and current.get("prev_hash"):
                import hashlib
                record_for_hash = {
                    k: v for k, v in current.items()
                    if k not in ["hash"]
                }
                from common.utils import calc_hash_chain
                recalc_hash = calc_hash_chain(current.get("prev_hash", ""), record_for_hash)
                if recalc_hash != current.get("hash"):
                    mismatches.append({
                        "index": i + 1,
                        "trace_id": current.get("trace_id", ""),
                        "operation": current.get("operation_type", ""),
                        "issue": (f"记录 hash 校验失败。"
                                 f"期望: {current.get('hash', '')[:16]}..., 重计算: {recalc_hash[:16]}...")
                    })
                    hash_chain_valid = False

        if len(audit_records) > 1:
            expected_seq = list(range(1, len(audit_records) + 1))
            actual_ops = [r.get("operation_type", "") for r in audit_records]

        return {
            "success": True,
            "release_id": release_id,
            "total_records": len(audit_records),
            "hash_chain_valid": hash_chain_valid,
            "operation_counts": operation_counts,
            "gaps": gaps,
            "mismatches": mismatches,
            "records": audit_records
        }

    def get_audit_verify_text(self, release_id: str) -> str:
        result = self.verify_audit_integrity(release_id)

        lines = []
        lines.append("=" * 80)
        lines.append("  CTMS 审计完整性核验报告")
        lines.append("=" * 80)
        lines.append(f"发布编号: {result['release_id']}")
        lines.append("")

        if not result.get("success"):
            lines.append(f"❌ 核验失败: {result.get('error', '未知错误')}")
            return "\n".join(lines)

        if result.get("note"):
            lines.append(f"ℹ  {result['note']}")
            return "\n".join(lines)

        lines.append(f"总审计记录数: {result['total_records']} 条")
        lines.append(f"哈希链完整性: {'✅ 完整' if result['hash_chain_valid'] else '❌ 存在问题'}")
        lines.append("")

        lines.append("【操作类型统计】")
        if result.get("operation_counts"):
            for op, count in sorted(result["operation_counts"].items()):
                lines.append(f"  {op:<30} {count:>3} 次")
        else:
            lines.append("  暂无统计数据")
        lines.append("")

        lines.append("【时间线与哈希校验】")
        lines.append(f"  {'序':<4} {'时间':<27} {'操作类型':<22} {'哈希状态':<10} 问题")
        lines.append("-" * 80)

        records = result.get("records", [])
        for i, rec in enumerate(records, 1):
            op = rec.get("operation_type", "")[:22]
            ts = rec.get("created_at", "")[:27]

            hash_status = "✅"
            issue_text = ""
            for mm in result.get("mismatches", []):
                if mm.get("index") == i:
                    hash_status = "❌"
                    issue_text = mm["issue"]
                    break

            for g in result.get("gaps", []):
                if g.get("index") == i:
                    issue_text = g["issue"]
                    break

            lines.append(f"  {i:<4} {ts:<27} {op:<22} {hash_status:<10} {issue_text}")

        lines.append("-" * 80)
        lines.append("")

        if result.get("mismatches"):
            lines.append(f"❌ 发现 {len(result['mismatches'])} 处哈希链问题:")
            for i, mm in enumerate(result["mismatches"], 1):
                lines.append(f"  [{i}] 第{mm['index']}条 ({mm['operation']}): {mm['issue']}")
            lines.append("")

        if result.get("gaps"):
            lines.append(f"⚠  发现 {len(result['gaps'])} 处时间线问题:")
            for i, g in enumerate(result["gaps"], 1):
                lines.append(f"  [{i}] {g['issue']}")
            lines.append("")

        if result.get("hash_chain_valid") and not result.get("gaps"):
            lines.append("✅ 审计记录完整，哈希链连续，无时间异常。")
        elif not result.get("hash_chain_valid") or result.get("gaps"):
            lines.append("⚠  审计记录存在问题，请核查。")

        lines.append("")
        lines.append("=" * 80)

        return "\n".join(lines)

    def compare_releases(self, release_id_a: str, release_id_b: str) -> Dict[str, Any]:
        rel_a = self.get_release(release_id_a)
        rel_b = self.get_release(release_id_b)

        if not rel_a or not rel_b:
            missing = []
            if not rel_a:
                missing.append(release_id_a)
            if not rel_b:
                missing.append(release_id_b)
            return {
                "success": False,
                "error": f"发布编号不存在: {', '.join(missing)}",
                "missing": missing
            }

        stats_a = rel_a.get("_stats", {})
        stats_b = rel_b.get("_stats", {})

        approval_a = rel_a.get("approval_flow", {})
        approval_b = rel_b.get("approval_flow", {})

        def calc_approval_duration(flow):
            if not flow or not flow.get("nodes"):
                return None
            nodes = flow.get("nodes", [])
            first_activated = None
            last_approved = None
            for n in nodes:
                if n.get("activated_at") and (first_activated is None or n["activated_at"] < first_activated):
                    first_activated = n["activated_at"]
                if n.get("approved_at") and (last_approved is None or n["approved_at"] > last_approved):
                    last_approved = n["approved_at"]
            if first_activated and last_approved:
                from datetime import datetime
                try:
                    dt1 = datetime.fromisoformat(first_activated)
                    dt2 = datetime.fromisoformat(last_approved)
                    hours = (dt2 - dt1).total_seconds() / 3600
                    return round(hours, 2)
                except Exception:
                    return None
            return None

        gray_a = rel_a.get("gray_plan", {})
        gray_b = rel_b.get("gray_plan", {})

        def count_something(plan, key):
            if not plan or not plan.get("batches"):
                return 0
            total = 0
            for b in plan.get("batches", []):
                if key == "fuse_events":
                    total += len(b.get("fuse_events", []))
                elif key == "rollback":
                    total += 1 if b.get("status") == "ROLLED_BACK" else 0
                elif key == "monitor_cycles":
                    cycles, _, _ = self._calc_monitor_stats(b)
                    total += cycles
            return total

        def get_risk_level(rel):
            try:
                report_dir = self.config["storage"]["report_dir"]
                report_json = os.path.join(report_dir, f"release_{rel['release_id']}_report.json")
                if os.path.exists(report_json):
                    with open(report_json, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    return data.get("conclusion", {}).get("risk_level", "N/A")
            except Exception:
                pass
            validation = rel.get("validation_result", {})
            if validation:
                if validation.get("high_risk_count", 0) > 0:
                    return "HIGH"
                elif validation.get("medium_risk_count", 0) > 0:
                    return "MEDIUM"
                elif validation.get("low_risk_count", 0) > 0:
                    return "LOW"
                else:
                    return "NONE"
            return "N/A"

        comparison = {
            "success": True,
            "release_a": {
                "release_id": rel_a["release_id"],
                "version": rel_a["version"],
                "created_at": rel_a["created_at"],
                "title": rel_a["title"],
                "type": "Hotfix" if rel_a.get("is_hotfix") else "Normal",
                "overall_status": rel_a["status"],
                "approval_progress": stats_a.get("approval_progress", "N/A"),
                "approval_duration_hours": calc_approval_duration(approval_a),
                "gray_batches": stats_a.get("total_batches", 0),
                "gray_current_batch": stats_a.get("current_batch", 0),
                "monitor_cycles": count_something(gray_a, "monitor_cycles"),
                "fuse_events": count_something(gray_a, "fuse_events"),
                "rollback_count": count_something(gray_a, "rollback"),
                "risk_level": get_risk_level(rel_a)
            },
            "release_b": {
                "release_id": rel_b["release_id"],
                "version": rel_b["version"],
                "created_at": rel_b["created_at"],
                "title": rel_b["title"],
                "type": "Hotfix" if rel_b.get("is_hotfix") else "Normal",
                "overall_status": rel_b["status"],
                "approval_progress": stats_b.get("approval_progress", "N/A"),
                "approval_duration_hours": calc_approval_duration(approval_b),
                "gray_batches": stats_b.get("total_batches", 0),
                "gray_current_batch": stats_b.get("current_batch", 0),
                "monitor_cycles": count_something(gray_b, "monitor_cycles"),
                "fuse_events": count_something(gray_b, "fuse_events"),
                "rollback_count": count_something(gray_b, "rollback"),
                "risk_level": get_risk_level(rel_b)
            }
        }

        a = comparison["release_a"]
        b = comparison["release_b"]
        comparison["analysis"] = []

        if a["approval_duration_hours"] and b["approval_duration_hours"]:
            diff = a["approval_duration_hours"] - b["approval_duration_hours"]
            if diff < 0:
                comparison["analysis"].append(
                    f"审批耗时: A比B快 {abs(diff):.2f} 小时 ✓"
                )
            elif diff > 0:
                comparison["analysis"].append(
                    f"审批耗时: A比B慢 {diff:.2f} 小时 ⚠"
                )
            else:
                comparison["analysis"].append("审批耗时: 两者相同")

        if a["fuse_events"] < b["fuse_events"]:
            comparison["analysis"].append(
                f"熔断次数: A({a['fuse_events']}次) 少于 B({b['fuse_events']}次) ✓"
            )
        elif a["fuse_events"] > b["fuse_events"]:
            comparison["analysis"].append(
                f"熔断次数: A({a['fuse_events']}次) 多于 B({b['fuse_events']}次) ⚠"
            )
        else:
            comparison["analysis"].append(f"熔断次数: 两者相同 ({a['fuse_events']}次)")

        if a["rollback_count"] < b["rollback_count"]:
            comparison["analysis"].append(
                f"回滚次数: A({a['rollback_count']}次) 少于 B({b['rollback_count']}次) ✓"
            )
        elif a["rollback_count"] > b["rollback_count"]:
            comparison["analysis"].append(
                f"回滚次数: A({a['rollback_count']}次) 多于 B({b['rollback_count']}次) ⚠"
            )
        else:
            comparison["analysis"].append(f"回滚次数: 两者相同 ({a['rollback_count']}次)")

        risk_map = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "N/A": -1}
        ra = risk_map.get(a["risk_level"], -1)
        rb = risk_map.get(b["risk_level"], -1)
        if ra >= 0 and rb >= 0:
            if ra < rb:
                comparison["analysis"].append(
                    f"风险等级: A({a['risk_level']}) 低于 B({b['risk_level']}) ✓"
                )
            elif ra > rb:
                comparison["analysis"].append(
                    f"风险等级: A({a['risk_level']}) 高于 B({b['risk_level']}) ⚠"
                )
            else:
                comparison["analysis"].append(f"风险等级: 两者相同 ({a['risk_level']})")

        return comparison

    def get_compare_text(self, release_id_a: str, release_id_b: str) -> str:
        result = self.compare_releases(release_id_a, release_id_b)

        lines = []
        lines.append("=" * 90)
        lines.append("  CTMS 多版本发布对比分析")
        lines.append("=" * 90)

        if not result.get("success"):
            lines.append(f"❌ 对比失败: {result.get('error', '未知错误')}")
            return "\n".join(lines)

        a = result["release_a"]
        b = result["release_b"]

        def fmt(val, unit=""):
            if val is None:
                return "N/A"
            return f"{val}{unit}"

        lines.append("")
        lines.append(f"{'对比项':<25} {'A: ' + a['release_id'][:18]:<32} {'B: ' + b['release_id'][:18]:<32}")
        lines.append("-" * 90)

        lines.append(f"{'版本号':<25} {fmt(a['version']):<32} {fmt(b['version']):<32}")
        lines.append(f"{'发布标题':<25} {a['title'][:30]:<32} {b['title'][:30]:<32}")
        lines.append(f"{'发布类型':<25} {fmt(a['type']):<32} {fmt(b['type']):<32}")
        lines.append(f"{'创建时间':<25} {a['created_at'][:30]:<32} {b['created_at'][:30]:<32}")
        lines.append(f"{'整体状态':<25} {fmt(a['overall_status']):<32} {fmt(b['overall_status']):<32}")
        lines.append(f"{'审批进度':<25} {fmt(a['approval_progress']):<32} {fmt(b['approval_progress']):<32}")
        lines.append(f"{'审批耗时':<25} {fmt(a['approval_duration_hours'], 'h'):<32} {fmt(b['approval_duration_hours'], 'h'):<32}")
        lines.append(f"{'灰度批次总数':<25} {fmt(a['gray_batches']):<32} {fmt(b['gray_batches']):<32}")
        lines.append(f"{'当前批次':<25} {fmt(a['gray_current_batch']):<32} {fmt(b['gray_current_batch']):<32}")
        lines.append(f"{'累计监控轮数':<25} {fmt(a['monitor_cycles'], '轮'):<32} {fmt(b['monitor_cycles'], '轮'):<32}")
        lines.append(f"{'熔断总次数':<25} {fmt(a['fuse_events'], '次'):<32} {fmt(b['fuse_events'], '次'):<32}")
        lines.append(f"{'回滚总次数':<25} {fmt(a['rollback_count'], '次'):<32} {fmt(b['rollback_count'], '次'):<32}")
        lines.append(f"{'风险等级':<25} {fmt(a['risk_level']):<32} {fmt(b['risk_level']):<32}")
        lines.append("-" * 90)
        lines.append("")

        lines.append("【对比分析结论】")
        if result.get("analysis"):
            for i, analysis in enumerate(result["analysis"], 1):
                lines.append(f"  {i}. {analysis}")
        else:
            lines.append("  暂无对比分析")

        lines.append("")
        lines.append("=" * 90)
        lines.append("  说明: ✓表示A更优, ⚠表示A需改进")
        lines.append("=" * 90)

        return "\n".join(lines)

    def get_snapshot_text(self, release_id: str) -> str:
        release = self.get_release(release_id)
        if not release:
            return f"错误：发布编号不存在 - {release_id}\n请使用 --list 命令查看所有发布"

        lines = []
        lines.append("=" * 80)
        lines.append("  CTMS 发布进度快照")
        lines.append("=" * 80)
        lines.append(f"生成时间: {get_now_iso()}")
        lines.append("")

        stats = release.get("_stats", {})
        lines.append("【基本信息】")
        lines.append(f"  发布编号: {release['release_id']}")
        lines.append(f"  版 本 号: {release['version']}")
        lines.append(f"  项目编号: {release['project_id']}")
        lines.append(f"  发布标题: {release['title']}")
        lines.append(f"  发布类型: {'紧急Hotfix' if release.get('is_hotfix') else '常规发布'}")
        lines.append(f"  申 请 人: {release['applicant']}")
        lines.append(f"  创建时间: {release['created_at']}")
        lines.append(f"  发布状态: {release['status']} → {stats.get('gray_status', 'N/A')}")
        lines.append("")

        lines.append("【审批链路】")
        approval_flow = release.get("approval_flow")
        if approval_flow and approval_flow.get("nodes"):
            lines.append(f"  审批进度: {stats.get('approval_progress', 'N/A')}")
            lines.append(f"  审批状态: {approval_flow.get('status', 'N/A')}")
            lines.append("")
            for node in approval_flow["nodes"]:
                status_icon = {"PENDING": "▶", "APPROVED": "✓", "REJECTED": "✗",
                               "POST_APPROVED": "✓*", "NOT_STARTED": "○"}.get(node["status"], "?")
                approver = node.get("approver", "N/A")
                time_str = node.get("approved_at", node.get("activated_at", node.get("created_at", "")))
                comment = f" - {node['comment']}" if node.get("comment") else ""
                lines.append(f"  {status_icon} 第{node['order']}步 {node['label']} [{node['status']}]")
                if time_str:
                    lines.append(f"      审批人: {approver} | 时间: {time_str}{comment}")
                else:
                    lines.append(f"      审批人: {approver} | 尚未处理")
        else:
            lines.append("  暂无审批链路数据")
        lines.append("")

        lines.append("【灰度批次进度】")
        gray_plan = release.get("gray_plan")
        if gray_plan and gray_plan.get("batches"):
            lines.append(f"  灰度阶段: {gray_plan.get('status', 'N/A')}")
            lines.append(f"  批次进度: 第{stats.get('current_batch', 0)}批 / 共{stats.get('total_batches', 0)}批")
            lines.append(f"  监控轮数: 累计 {stats.get('total_cycles', 0)} 轮")
            lines.append(f"  熔断次数: 累计 {stats.get('total_fuse', 0)} 次")
            lines.append("")
            for batch in gray_plan["batches"]:
                status_icon = {"PENDING": "○", "RELEASING": "→", "OBSERVING": "▶",
                               "COMPLETED": "✓", "ROLLED_BACK": "✗"}.get(batch["status"], "?")
                cycles, n_metrics, n_records = self._calc_monitor_stats(batch)
                fuse_count = len(batch.get("fuse_events", []))

                lines.append(f"  {status_icon} 第{batch['batch_no']}批 ({batch['label']}) - [{batch['status']}]")
                lines.append(f"      中心数: {batch['center_count']} | 观察期: {batch['observation_hours']}h")
                if batch.get("release_time"):
                    lines.append(f"      发布时间: {batch['release_time']}")
                lines.append(f"      监控: {cycles}轮 | 熔断: {fuse_count}次")

                if fuse_count > 0:
                    for fe in batch["fuse_events"]:
                        reason_names = ", ".join([r.get("metric", "") for r in fe.get("reasons", [])[:3]])
                        lines.append(f"        ⚠ {fe['fuse_level_name']} @ {fe['trigger_time']}: {reason_names}")
                lines.append("")
        else:
            lines.append("  暂无灰度发布数据")
        lines.append("")

        lines.append("【最近一轮监控指标】")
        if gray_plan and gray_plan.get("batches"):
            found_data = False
            for batch in gray_plan["batches"]:
                if batch["status"] == "PENDING":
                    continue
                metrics = batch.get("monitor_metrics", [])
                if not metrics:
                    continue
                found_data = True
                latest_by_name = {}
                for m in metrics:
                    name = m["metric_name"]
                    if name not in latest_by_name or m["collected_at"] > latest_by_name[name]["collected_at"]:
                        latest_by_name[name] = m

                lines.append(f"  第{batch['batch_no']}批 ({batch['label']}):")
                for name in sorted(latest_by_name.keys()):
                    m = latest_by_name[name]
                    status_icon = {"NORMAL": " ", "WARN": "!", "FUSE": "✗"}.get(m["status"], "?")
                    unit = m.get("unit", "")
                    lines.append(f"    {status_icon} {m['metric_label']}: {m['metric_value']}{unit}")
            if not found_data:
                lines.append("  暂无监控指标数据")
        else:
            lines.append("  暂无监控指标数据")
        lines.append("")

        lines.append("【熔断记录汇总】")
        if stats.get("total_fuse", 0) > 0:
            all_events = []
            for batch in gray_plan["batches"]:
                for event in batch.get("fuse_events", []):
                    all_events.append({"batch_no": batch["batch_no"], "batch_label": batch["label"], **event})
            for i, ev in enumerate(all_events, 1):
                reasons = ", ".join([f"{r['metric']}={r['value']}" for r in ev.get("reasons", [])[:3]])
                lines.append(f"  [{i}] 第{ev['batch_no']}批 - {ev['fuse_level_name']} @ {ev['trigger_time']}")
                lines.append(f"      触发指标: {reasons}")
        else:
            lines.append("  暂无熔断记录")
        lines.append("")

        lines.append("【回滚信息】")
        if release.get("status") == "ROLLED_BACK" or (
            gray_plan and any(b.get("status") == "ROLLED_BACK" for b in gray_plan.get("batches", []))
        ):
            if gray_plan:
                for batch in gray_plan["batches"]:
                    if batch.get("status") == "ROLLED_BACK" and batch.get("rollback_time"):
                        lines.append(f"  第{batch['batch_no']}批 回滚完成")
                        lines.append(f"      回滚时间: {batch['rollback_time']}")
                        if batch.get("rollback_version"):
                            lines.append(f"      回滚版本: {batch['rollback_version']}")
        else:
            lines.append("  暂无回滚记录")
        lines.append("")

        lines.append("【关键审计流水】")
        try:
            audit_records = self.audit_logger.query(target_type="release", target_id=release_id)
            audit_records = audit_records[:15]
            if audit_records:
                for i, rec in enumerate(audit_records, 1):
                    op = rec.get("operation_type", "N/A")
                    operator = rec.get("operator", "N/A")
                    ts = rec.get("created_at", "")
                    remark = rec.get("remark", "")
                    after = rec.get("after_value", {})
                    before = rec.get("before_value", {})

                    if remark:
                        detail = remark[:50]
                    elif after:
                        if isinstance(after, dict):
                            parts = []
                            for k, v in list(after.items())[:3]:
                                parts.append(f"{k}={v}")
                            detail = ", ".join(parts)[:50]
                        else:
                            detail = str(after)[:50]
                    elif before:
                        if isinstance(before, dict):
                            parts = []
                            for k, v in list(before.items())[:3]:
                                parts.append(f"{k}={v}")
                            detail = ", ".join(parts)[:50]
                        else:
                            detail = str(before)[:50]
                    else:
                        detail = "无详情"

                    result_status = "✓ 成功"
                    if "FAIL" in op or "REJECT" in op:
                        result_status = "✗ 失败"
                    elif "FUSE" in op or "WARN" in op:
                        result_status = "⚠ 告警"
                    elif "ROLLBACK" in op:
                        result_status = "↺ 回滚"

                    lines.append(f"  [{i:02d}] {ts} | {operator:<10} | {op:<22} | {result_status:<10} {detail}")
            else:
                lines.append("  暂无审计记录")
        except Exception as e:
            lines.append(f"  审计查询失败: {e}")

        lines.append("")
        lines.append("=" * 80)
        lines.append("  — 进度快照结束，请复制以上内容同步项目组 —")
        lines.append("=" * 80)

        return "\n".join(lines)

    def generate_sync_package(self, release_id: str, output_dir: str = None) -> Dict[str, Any]:
        release = self.get_release(release_id)
        if not release:
            raise ValueError(f"发布编号不存在: {release_id}")

        if not output_dir:
            output_dir = os.path.join(self.config["storage"]["data_dir"], "sync_packages")
        ensure_dir(output_dir)

        stats = release.get("_stats", {})
        gray_plan = release.get("gray_plan")
        approval_flow = release.get("approval_flow")

        package_data = {
            "package_generated_at": get_now_iso(),
            "release_overview": {
                "release_id": release["release_id"],
                "version": release["version"],
                "project_id": release["project_id"],
                "title": release["title"],
                "description": release.get("description", "暂无"),
                "release_type": "紧急Hotfix" if release.get("is_hotfix") else "常规发布",
                "applicant": release["applicant"],
                "created_at": release["created_at"],
                "overall_status": release["status"],
                "gray_stage": stats.get("gray_status", "暂无"),
                "current_stage": release.get("current_stage", "暂无")
            },
            "approval_chain": {},
            "gray_progress": {},
            "monitor_summary": {},
            "fuse_rollback": {},
            "audit_timeline": [],
            "postmortem_summary": {}
        }

        if approval_flow and approval_flow.get("nodes"):
            nodes = []
            for node in approval_flow["nodes"]:
                nodes.append({
                    "order": node["order"],
                    "label": node["label"],
                    "name": node["name"],
                    "approver": node.get("approver", "暂无"),
                    "status": node["status"],
                    "approved_at": node.get("approved_at", "暂无"),
                    "activated_at": node.get("activated_at", "暂无"),
                    "comment": node.get("comment", "暂无"),
                    "is_post_approval": node.get("is_post_approval", False)
                })
            package_data["approval_chain"] = {
                "status": approval_flow.get("status", "暂无"),
                "progress": stats.get("approval_progress", "0/0"),
                "nodes": nodes
            }
        else:
            package_data["approval_chain"] = {"status": "暂无", "progress": "0/0", "nodes": [], "note": "暂无审批链路数据"}

        if gray_plan and gray_plan.get("batches"):
            batches = []
            total_cycles = 0
            total_fuse = 0
            for batch in gray_plan["batches"]:
                cycles, n_metrics, n_records = self._calc_monitor_stats(batch)
                total_cycles += cycles
                total_fuse += len(batch.get("fuse_events", []))
                fuse_events = []
                for fe in batch.get("fuse_events", []):
                    fuse_events.append({
                        "fuse_level": fe.get("fuse_level"),
                        "fuse_level_name": fe.get("fuse_level_name"),
                        "trigger_time": fe.get("trigger_time"),
                        "reasons": fe.get("reasons", [])
                    })
                batches.append({
                    "batch_no": batch["batch_no"],
                    "label": batch["label"],
                    "level": batch.get("level", ""),
                    "center_count": batch["center_count"],
                    "status": batch["status"],
                    "observation_hours": batch["observation_hours"],
                    "release_time": batch.get("release_time", "暂无"),
                    "observation_start_time": batch.get("observation_start_time", "暂无"),
                    "monitor_cycles": cycles,
                    "metric_types": n_metrics,
                    "total_records": n_records,
                    "fuse_events": fuse_events,
                    "rollback_time": batch.get("rollback_time", "暂无"),
                    "rollback_version": batch.get("rollback_version", "暂无")
                })

            package_data["gray_progress"] = {
                "status": gray_plan.get("status", "暂无"),
                "current_batch": stats.get("current_batch", 0),
                "total_batches": stats.get("total_batches", 0),
                "total_centers": gray_plan.get("total_centers", 0),
                "released_centers": gray_plan.get("released_centers_count", 0),
                "total_monitor_cycles": total_cycles,
                "total_fuse_events": total_fuse,
                "batches": batches
            }
        else:
            package_data["gray_progress"] = {"note": "暂无灰度发布数据"}

        if gray_plan and gray_plan.get("batches"):
            monitor_data = {}
            for batch in gray_plan["batches"]:
                metrics = batch.get("monitor_metrics", [])
                if metrics:
                    latest_by_name = {}
                    for m in metrics:
                        name = m["metric_name"]
                        if name not in latest_by_name or m["collected_at"] > latest_by_name[name]["collected_at"]:
                            latest_by_name[name] = m
                    monitor_data[f"batch_{batch['batch_no']}"] = {
                        "batch_label": batch["label"],
                        "latest_metrics": [
                            {
                                "name": m["metric_name"],
                                "label": m["metric_label"],
                                "value": m["metric_value"],
                                "unit": m.get("unit", ""),
                                "status": m["status"],
                                "collected_at": m["collected_at"]
                            } for m in sorted(latest_by_name.values(), key=lambda x: x["metric_name"])
                        ]
                    }
            package_data["monitor_summary"] = monitor_data if monitor_data else {"note": "暂无监控数据"}
        else:
            package_data["monitor_summary"] = {"note": "暂无监控数据"}

        fuse_events = []
        rollback_events = []
        if gray_plan and gray_plan.get("batches"):
            for batch in gray_plan["batches"]:
                for fe in batch.get("fuse_events", []):
                    fuse_events.append({
                        "batch_no": batch["batch_no"],
                        "batch_label": batch["label"],
                        "fuse_level": fe.get("fuse_level"),
                        "fuse_level_name": fe.get("fuse_level_name"),
                        "trigger_time": fe.get("trigger_time"),
                        "reasons": fe.get("reasons", [])
                    })
                if batch.get("status") == "ROLLED_BACK":
                    rollback_events.append({
                        "batch_no": batch["batch_no"],
                        "batch_label": batch["label"],
                        "rollback_time": batch.get("rollback_time"),
                        "rollback_version": batch.get("rollback_version"),
                        "rollback_operator": batch.get("rollback_operator", "system")
                    })

        package_data["fuse_rollback"] = {
            "total_fuse_events": len(fuse_events),
            "total_rollback_events": len(rollback_events),
            "fuse_events": fuse_events if fuse_events else [],
            "rollback_events": rollback_events if rollback_events else [],
            "note_fuse": "暂无熔断记录" if not fuse_events else "",
            "note_rollback": "暂无回滚记录" if not rollback_events else ""
        }

        try:
            audit_records = self.audit_logger.query(target_type="release", target_id=release_id)
            for rec in audit_records:
                op = rec.get("operation_type", "N/A")
                result_status = "✓ 成功"
                if "FAIL" in op or "REJECT" in op:
                    result_status = "✗ 失败"
                elif "FUSE" in op or "WARN" in op:
                    result_status = "⚠ 告警"
                elif "ROLLBACK" in op:
                    result_status = "↺ 回滚"

                remark = rec.get("remark", "")
                after = rec.get("after_value", {})
                if remark:
                    detail = remark
                elif after and isinstance(after, dict):
                    parts = [f"{k}={v}" for k, v in list(after.items())[:3]]
                    detail = ", ".join(parts)
                else:
                    detail = "无详情"

                package_data["audit_timeline"].append({
                    "seq": len(package_data["audit_timeline"]) + 1,
                    "time": rec.get("created_at", ""),
                    "operation_type": op,
                    "operator": rec.get("operator", ""),
                    "result": result_status,
                    "detail": detail,
                    "trace_id": rec.get("trace_id", "")
                })
        except Exception as e:
            package_data["audit_timeline"] = [{"note": f"审计查询失败: {e}"}]

        if not package_data["audit_timeline"]:
            package_data["audit_timeline"] = [{"note": "暂无审计记录"}]

        try:
            report_text_path = os.path.join(
                self.report_generator.report_dir,
                f"release_{release_id}_report.txt"
            )
            if os.path.exists(report_text_path):
                report_json_path = os.path.join(
                    self.report_generator.report_dir,
                    f"release_{release_id}_report.json"
                )
                if os.path.exists(report_json_path):
                    with open(report_json_path, 'r', encoding='utf-8') as f:
                        report_data = json.load(f)
                    package_data["postmortem_summary"] = {
                        "conclusion": report_data.get("conclusion", {}).get("overall_assessment", "暂无"),
                        "risk_level": report_data.get("conclusion", {}).get("risk_level", "暂无"),
                        "has_blocking_issues": report_data.get("conclusion", {}).get("has_blocking_issues", False),
                        "recommendations": report_data.get("conclusion", {}).get("recommendations", []),
                        "lessons_learned": report_data.get("conclusion", {}).get("lessons_learned", "暂无"),
                        "report_generated": True
                    }
                else:
                    with open(report_text_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                    lines = text.split('\n')[:50]
                    package_data["postmortem_summary"] = {
                        "report_preview": "\n".join(lines),
                        "report_generated": True,
                        "note": "报告以文本预览方式提供"
                    }
            else:
                package_data["postmortem_summary"] = {"note": "暂无复盘报告", "report_generated": False}
        except Exception as e:
            package_data["postmortem_summary"] = {"note": f"复盘报告读取失败: {e}", "report_generated": False}

        md_content = self._package_to_markdown(package_data)

        json_path = os.path.join(output_dir, f"sync_{release_id}.json")
        md_path = os.path.join(output_dir, f"sync_{release_id}.md")

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(package_data, f, ensure_ascii=False, indent=2)

        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        return {
            "success": True,
            "release_id": release_id,
            "json_path": json_path,
            "markdown_path": md_path,
            "output_dir": output_dir
        }

    def _package_to_markdown(self, pkg: Dict[str, Any]) -> str:
        lines = []
        lines.append(f"# CTMS 发布项目同步包")
        lines.append("")
        lines.append(f"> 生成时间: {pkg['package_generated_at']}")
        lines.append(f"> 发布编号: {pkg['release_overview']['release_id']}")
        lines.append(f"> 版本: {pkg['release_overview']['version']}")
        lines.append("")

        lines.append("## 1. 发布概览")
        lines.append("")
        ov = pkg["release_overview"]
        lines.append("| 项目 | 内容 |")
        lines.append("|------|------|")
        lines.append(f"| 发布编号 | {ov['release_id']} |")
        lines.append(f"| 版本号 | {ov['version']} |")
        lines.append(f"| 项目编号 | {ov['project_id']} |")
        lines.append(f"| 发布标题 | {ov['title']} |")
        lines.append(f"| 发布描述 | {ov['description']} |")
        lines.append(f"| 发布类型 | {ov['release_type']} |")
        lines.append(f"| 申请人 | {ov['applicant']} |")
        lines.append(f"| 创建时间 | {ov['created_at']} |")
        lines.append(f"| 整体状态 | {ov['overall_status']} |")
        lines.append(f"| 灰度阶段 | {ov['gray_stage']} |")
        lines.append(f"| 当前阶段 | {ov['current_stage']} |")
        lines.append("")

        lines.append("## 2. 审批链路")
        lines.append("")
        ac = pkg["approval_chain"]
        lines.append(f"- 审批状态: **{ac['status']}**")
        lines.append(f"- 审批进度: **{ac['progress']}**")
        lines.append("")
        if ac.get("nodes"):
            lines.append("| 序号 | 节点 | 审批人 | 状态 | 审批时间 | 意见 |")
            lines.append("|------|------|--------|------|----------|------|")
            for node in ac["nodes"]:
                status_icon = {"PENDING": "▶待办", "APPROVED": "✓通过", "REJECTED": "✗拒绝",
                               "POST_APPROVED": "✓补签", "NOT_STARTED": "○未启动"}.get(node["status"], node["status"])
                lines.append(f"| {node['order']} | {node['label']} | {node['approver']} | {status_icon} | {node['approved_at']} | {node['comment']} |")
        else:
            lines.append("> 暂无审批链路数据")
        lines.append("")

        lines.append("## 3. 灰度进度")
        lines.append("")
        gp = pkg["gray_progress"]
        if "note" in gp:
            lines.append(f"> {gp['note']}")
        else:
            lines.append(f"- 灰度状态: **{gp['status']}**")
            lines.append(f"- 批次进度: 第{gp['current_batch']}批 / 共{gp['total_batches']}批")
            lines.append(f"- 中心总数: {gp['total_centers']} 个")
            lines.append(f"- 已发布中心: {gp['released_centers']} 个")
            lines.append(f"- 累计监控轮数: {gp['total_monitor_cycles']} 轮")
            lines.append(f"- 累计熔断次数: {gp['total_fuse_events']} 次")
            lines.append("")
            lines.append("### 批次详情")
            lines.append("")
            lines.append("| 批次 | 类型 | 中心数 | 状态 | 发布时间 | 监控轮数 | 熔断 | 回滚 |")
            lines.append("|------|------|--------|------|----------|----------|------|------|")
            for b in gp["batches"]:
                fuse_icon = f"⚠{len(b['fuse_events'])}次" if b["fuse_events"] else "-"
                rollback_icon = "✗已回滚" if b["status"] == "ROLLED_BACK" else "-"
                lines.append(f"| {b['batch_no']} | {b['label']} | {b['center_count']} | {b['status']} | {b['release_time']} | {b['monitor_cycles']}轮 | {fuse_icon} | {rollback_icon} |")
        lines.append("")

        lines.append("## 4. 监控摘要")
        lines.append("")
        ms = pkg["monitor_summary"]
        if "note" in ms:
            lines.append(f"> {ms['note']}")
        else:
            for batch_key, batch_data in ms.items():
                lines.append(f"### {batch_data['batch_label']}")
                lines.append("")
                lines.append("| 指标 | 标签 | 最新值 | 状态 | 采集时间 |")
                lines.append("|------|------|--------|------|----------|")
                for m in batch_data["latest_metrics"]:
                    status_icon = {"NORMAL": "✓", "WARN": "!", "FUSE": "✗"}.get(m["status"], "?")
                    lines.append(f"| {m['name']} | {m['label']} | {m['value']}{m['unit']} | {status_icon} {m['status']} | {m['collected_at']} |")
                lines.append("")
        lines.append("")

        lines.append("## 5. 熔断与回滚")
        lines.append("")
        fr = pkg["fuse_rollback"]
        lines.append(f"- 累计熔断次数: **{fr['total_fuse_events']}** 次")
        lines.append(f"- 累计回滚次数: **{fr['total_rollback_events']}** 次")
        lines.append("")
        if fr["fuse_events"]:
            lines.append("### 熔断记录")
            lines.append("")
            lines.append("| 批次 | 级别 | 触发时间 | 触发指标 |")
            lines.append("|------|------|----------|----------|")
            for fe in fr["fuse_events"]:
                reasons = ", ".join([f"{r['metric']}={r['value']}" for r in fe["reasons"][:2]])
                lines.append(f"| {fe['batch_no']} | {fe['fuse_level_name']} | {fe['trigger_time']} | {reasons} |")
            lines.append("")
        else:
            lines.append("> 暂无熔断记录")
            lines.append("")

        if fr["rollback_events"]:
            lines.append("### 回滚记录")
            lines.append("")
            lines.append("| 批次 | 回滚时间 | 回滚版本 | 操作者 |")
            lines.append("|------|----------|----------|--------|")
            for re_ev in fr["rollback_events"]:
                lines.append(f"| {re_ev['batch_no']} | {re_ev['rollback_time']} | {re_ev['rollback_version']} | {re_ev['rollback_operator']} |")
            lines.append("")
        else:
            lines.append("> 暂无回滚记录")
            lines.append("")

        lines.append("## 6. 审计时间线")
        lines.append("")
        at = pkg["audit_timeline"]
        if at and "note" not in at[0]:
            lines.append("| 序号 | 时间 | 操作 | 操作者 | 结果 | 详情 |")
            lines.append("|------|------|------|--------|------|------|")
            for rec in at:
                lines.append(f"| {rec['seq']} | {rec['time']} | {rec['operation_type']} | {rec['operator']} | {rec['result']} | {rec['detail']} |")
        else:
            note = at[0]["note"] if at else "暂无审计记录"
            lines.append(f"> {note}")
        lines.append("")

        lines.append("## 7. 复盘报告摘要")
        lines.append("")
        ps = pkg["postmortem_summary"]
        if ps.get("report_generated"):
            if "conclusion" in ps:
                lines.append(f"- 总体评估: **{ps['conclusion']}**")
                lines.append(f"- 风险等级: **{ps['risk_level']}**")
                lines.append(f"- 阻塞性问题: {'是' if ps['has_blocking_issues'] else '否'}")
                lines.append("")
                if ps.get("recommendations"):
                    lines.append("### 改进建议")
                    lines.append("")
                    for i, rec in enumerate(ps["recommendations"], 1):
                        lines.append(f"{i}. {rec}")
                    lines.append("")
                lines.append(f"### 经验教训")
                lines.append("")
                lines.append(f"> {ps['lessons_learned']}")
            else:
                lines.append("```")
                lines.append(ps.get("report_preview", "暂无报告预览"))
                lines.append("```")
        else:
            lines.append(f"> {ps.get('note', '暂无复盘报告')}")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("> 本同步包由 CTMS 发布平台自动生成，包含发布审批流程的完整记录。")
        lines.append("> 如需进一步了解详情，请使用 `python release_orchestrator.py --release-id <编号> --all-status` 查询。")

        return "\n".join(lines)

    def get_timeline_text(self, release_id: str) -> str:
        release = self.get_release(release_id)
        if not release:
            return f"错误：发布编号不存在 - {release_id}\n请使用 --list 命令查看所有发布"

        events = []

        events.append({
            "time": release.get("created_at", ""),
            "type": "创建申请",
            "operator": release.get("applicant", "system"),
            "result": "✓ 成功",
            "detail": f"创建版本 {release['version']}，发布类型: {'Hotfix' if release.get('is_hotfix') else '常规'}"
        })

        validation = release.get("validation_result")
        if validation:
            events.append({
                "time": validation.get("checked_at", validation.get("validated_at", "")),
                "type": "发布校验",
                "operator": validation.get("validator", validation.get("operator", "system")),
                "result": f"{'✓ 通过' if validation.get('summary', {}).get('passed', validation.get('passed')) else '✗ 未通过'}",
                "detail": (f"高危: {validation.get('high_risk_count', validation.get('summary', {}).get('blocked_count', 0))}, "
                          f"中危: {validation.get('medium_risk_count', 0)}, "
                          f"低危: {validation.get('low_risk_count', 0)}")
            })

        approval_flow = release.get("approval_flow")
        if approval_flow and approval_flow.get("nodes"):
            for node in approval_flow["nodes"]:
                node_time = node.get("approved_at", node.get("activated_at", ""))
                if node["status"] in ["APPROVED", "POST_APPROVED"]:
                    events.append({
                        "time": node_time,
                        "type": f"审批: {node['label']}",
                        "operator": node.get("approver", ""),
                        "result": f"✓ {'补签' if node['status'] == 'POST_APPROVED' else '通过'}",
                        "detail": node.get("comment", "")
                    })
                elif node["status"] == "REJECTED":
                    events.append({
                        "time": node_time,
                        "type": f"审批: {node['label']}",
                        "operator": node.get("approver", ""),
                        "result": "✗ 拒绝",
                        "detail": node.get("comment", "")
                    })

        gray_plan = release.get("gray_plan")
        if gray_plan and gray_plan.get("batches"):
            for batch in gray_plan["batches"]:
                if batch.get("release_time"):
                    events.append({
                        "time": batch["release_time"],
                        "type": f"灰度启动: 第{batch['batch_no']}批",
                        "operator": batch.get("operator", "system"),
                        "result": "✓ 成功",
                        "detail": f"{batch['center_count']}个中心 - {batch['label']}"
                    })
                if batch.get("observation_start_time"):
                    events.append({
                        "time": batch["observation_start_time"],
                        "type": f"进入观察期: 第{batch['batch_no']}批",
                        "operator": "system",
                        "result": f"▶ 观察中 ({batch['observation_hours']}h)",
                        "detail": f"{batch['label']}"
                    })

                monitor_times = set()
                for m in batch.get("monitor_metrics", []):
                    monitor_times.add(m["collected_at"])
                for i, mt in enumerate(sorted(monitor_times), 1):
                    batch_metrics = [m for m in batch.get("monitor_metrics", [])
                                     if m["collected_at"] == mt]
                    abnormal = sum(1 for m in batch_metrics if m["status"] != "NORMAL")
                    result_text = f"✓ 正常" if abnormal == 0 else f"⚠ {abnormal}项异常"
                    events.append({
                        "time": mt,
                        "type": f"监控采集: 第{batch['batch_no']}批-第{i}轮",
                        "operator": "system",
                        "result": result_text,
                        "detail": f"{len(batch_metrics)}项指标"
                    })

                for fe in batch.get("fuse_events", []):
                    level_icon = {1: "⚠ 一级", 2: "⚠ 二级", 3: "⚠ 三级"}.get(fe.get("fuse_level"), "⚠")
                    reason_text = ", ".join([r["metric"] for r in fe.get("reasons", [])[:2]])
                    events.append({
                        "time": fe.get("trigger_time", ""),
                        "type": f"熔断触发: 第{batch['batch_no']}批",
                        "operator": "system",
                        "result": f"{level_icon}预警",
                        "detail": f"{fe['fuse_level_name']} - 指标: {reason_text}"
                    })

                if batch.get("rollback_time"):
                    events.append({
                        "time": batch["rollback_time"],
                        "type": f"执行回滚: 第{batch['batch_no']}批",
                        "operator": batch.get("rollback_operator", "system"),
                        "result": "✗ 回滚",
                        "detail": f"回滚至版本: {batch.get('rollback_version', 'N/A')}"
                    })

                if batch["status"] == "COMPLETED" and batch.get("completed_at"):
                    events.append({
                        "time": batch.get("completed_at", ""),
                        "type": f"批次完成: 第{batch['batch_no']}批",
                        "operator": "system",
                        "result": "✓ 完成",
                        "detail": f"{batch['label']}观察期结束"
                    })

        report_path = os.path.join(
            self.report_generator.report_dir,
            f"release_{release_id}_report.txt"
        )
        if os.path.exists(report_path):
            report_mtime = get_now_iso()
            try:
                report_mtime = datetime.datetime.fromtimestamp(
                    os.path.getmtime(report_path), tz=datetime.timezone.utc
                ).isoformat()
            except Exception:
                pass
            events.append({
                "time": report_mtime,
                "type": "生成复盘报告",
                "operator": "system",
                "result": "✓ 已生成",
                "detail": os.path.basename(report_path)
            })

        events.sort(key=lambda x: x["time"])

        lines = []
        lines.append("=" * 80)
        lines.append("  CTMS 发布合规时间线")
        lines.append("=" * 80)
        lines.append(f"发布编号: {release['release_id']}")
        lines.append(f"版本: {release['version']} | 项目: {release['project_id']}")
        lines.append(f"当前状态: {release['status']}")
        lines.append("")
        lines.append(f"  {'时间':<26} {'操作类型':<22} {'操作者':<10} {'结果':<10} 详情")
        lines.append("-" * 80)

        for i, ev in enumerate(events, 1):
            time_str = ev["time"][:26] if len(ev["time"]) > 26 else ev["time"]
            op_type = ev["type"][:22]
            operator = str(ev["operator"])[:10]
            result = str(ev["result"])[:10]
            detail = str(ev["detail"])[:40]
            lines.append(f"{i:02d}|{time_str:<26} {op_type:<22} {operator:<10} {result:<10} {detail}")

        lines.append("-" * 80)
        lines.append(f"  共 {len(events)} 条事件记录")
        lines.append("=" * 80)

        return "\n".join(lines)


def run_demo():
    print("\n" + "=" * 70)
    print("  CTMS 系统版本发布与智能回滚自动化平台 — 完整流程演示")
    print("=" * 70)

    orchestrator = ReleaseOrchestrator()

    print("\n" + "-" * 70)
    print("步骤 1: 创建发布申请")
    print("-" * 70)

    release = orchestrator.create_release_request(
        version="2.3.0",
        project_id="CT-2024-001",
        title="CTMS V2.3.0 功能迭代发布",
        description="新增受试者随访管理模块，优化EDC数据同步性能",
        applicant="dev_li",
        is_hotfix=False
    )
    print(f"  发布编号: {release['release_id']}")
    print(f"  版 本 号: {release['version']}")
    print(f"  发布类型: {'紧急Hotfix' if release['is_hotfix'] else '常规发布'}")
    print(f"  当前状态: {release['status']}")

    release_id = release["release_id"]

    print("\n" + "-" * 70)
    print("步骤 2: 提交前置校验")
    print("-" * 70)

    mock_validation_data = {
        "edc_sync": {
            "success_rate": 99.8,
            "total_syncs": 1250,
            "failed_syncs": 3
        },
        "subject_fields": {
            "match_rate": 100.0,
            "total_fields": 50,
            "mismatch_fields": []
        },
        "open_queries": {
            "count": 2
        },
        "ethics_approvals": {
            "total_centers": 10,
            "valid_count": 10,
            "expired_list": []
        },
        "icf_versions": {
            "consistent": True,
            "details": []
        },
        "sae_reports": {
            "unreported_count": 0,
            "details": []
        },
        "milestones": {
            "max_deviation_days": 2,
            "details": []
        },
        "budget_enrollment": {
            "enrollment_rate": 45.0,
            "budget_rate": 52.0
        },
        "center_stall": {
            "stalled_count": 0,
            "details": []
        },
        "tmf_documents": {
            "completeness_rate": 100.0,
            "total_core_docs": 80,
            "archived_docs": 80,
            "missing_docs": []
        },
        "esignatures": {
            "all_valid": True,
            "invalid_docs": []
        },
        "audit_trail": {
            "complete": True,
            "issues": []
        }
    }

    release = orchestrator.submit_for_validation(release_id, mock_validation_data)
    val_result = release["validation_result"]
    print(f"  校验状态: {'阻断' if val_result['summary']['blocked'] else '通过'}")
    print(f"  通过项: {val_result['summary']['passed']} / {val_result['summary']['total']}")
    print(f"  失败项: {val_result['summary']['failed']}")
    print(f"  警告项: {val_result['summary']['warnings']}")

    if val_result["summary"]["blocked"]:
        print("\n  阻断项:")
        for i, item in enumerate(val_result["summary"]["blocking_items"], 1):
            print(f"    {i}. {item['label']}")
            if item.get("suggestion"):
                print(f"       建议: {item['suggestion']}")

    print("\n" + "-" * 70)
    print("步骤 3: 分级审批流转")
    print("-" * 70)

    print("\n  3.1 临床审批通过")
    release = orchestrator.approve_node(
        release_id, "clinical", "dr_zhang",
        "经评估，本次发布不涉及受试者安全风险，方案执行无影响。"
    )
    print(f"    状态: {release['approval_flow']['status']}")

    print("\n  3.2 数据审批通过")
    release = orchestrator.approve_node(
        release_id, "data", "dm_li",
        "数据迁移方案已评审，EDC同步接口兼容性验证通过。"
    )
    print(f"    状态: {release['approval_flow']['status']}")

    print("\n  3.3 质控审批通过")
    release = orchestrator.approve_node(
        release_id, "qa", "qa_wang",
        "GCP合规性评估通过，TMF文档已同步更新。"
    )
    print(f"    状态: {release['approval_flow']['status']}")

    print("\n  3.4 PM审批通过")
    release = orchestrator.approve_node(
        release_id, "pm", "pm_chen",
        "发布窗口确认，资源协调到位，同意发布。"
    )
    print(f"    审批全部通过! 发布状态: {release['status']}")

    print("\n" + "-" * 70)
    print("步骤 4: 灰度发布（三批次逐步放量）")
    print("-" * 70)

    centers = []
    for i in range(1, 26):
        enrolling = 0
        is_hub = False
        if i <= 3:
            enrolling = 80 + i * 5
            is_hub = True
        elif i <= 13:
            enrolling = 15 + i * 2
        else:
            enrolling = i - 13

        centers.append({
            "id": f"C{i:03d}",
            "name": f"研究中心{i}",
            "enrolling_count": enrolling,
            "is_hub": is_hub
        })

    print(f"  共 {len(centers)} 个研究中心参与发布")

    release = orchestrator.start_gray_release(release_id, centers)
    gray_plan = release["gray_plan"]
    print(f"\n  灰度计划:")
    print(f"    总批次数: {gray_plan['batch_count']}")
    for batch in gray_plan["batches"]:
        print(f"    第{batch['batch_no']}批 ({batch['label']}): {batch['center_count']}个中心")

    print("\n" + "-" * 70)
    print("步骤 5: 实时监控 & 自动熔断模拟")
    print("-" * 70)

    release = orchestrator.complete_current_batch(release_id, success=True)
    print(f"\n  第一批发布完成，进入观察期")

    print("\n  模拟 5 轮监控采集（每 5 分钟一轮）:")
    for i in range(5):
        print(f"\n  第 {i+1} 轮监控:")

        if i == 3:
            mock_metrics = {
                "data_anomaly_rate": 6.5,
                "entry_delay_rate": 12.0,
                "approval_block_rate": 10.0,
                "system_error_rate": 0.8,
                "login_success_rate": 99.0
            }
        else:
            mock_metrics = None

        result = orchestrator.monitor_and_check(release_id, mock_metrics)

        for metric in result["metrics"]:
            status_icon = "✓" if metric["status"] == "NORMAL" else "⚠" if metric["status"] == "WARN" else "✗"
            print(f"    {status_icon} {metric['metric_label']}: {metric['metric_value']}{metric['unit']} "
                  f"(预警: {metric['warn_threshold']}{metric['unit']}, "
                  f"熔断: {metric['fuse_threshold']}{metric['unit']})")

        if result["fuse"]["triggered"]:
            print(f"\n  ⚠️  熔断触发: {result['fuse']['fuse_level_name']}")
            for reason in result["fuse"]["trigger_reasons"]:
                print(f"    - {reason['metric']}: {reason['value']} (阈值: {reason['threshold']})")
            if result.get("rollback"):
                print(f"  🔄 已自动执行回滚: {result['rollback']['scope']}")

    print("\n" + "-" * 70)
    print("步骤 6: 生成发布复盘报告")
    print("-" * 70)

    report = orchestrator.generate_report(release_id)
    report_file = os.path.join(
        orchestrator.report_generator.report_dir,
        f"release_{release_id}_report.txt"
    )
    print(f"  报告已生成: {report_file}")

    print("\n  报告摘要:")
    print(f"    发布版本: {report['overview']['version']}")
    print(f"    发布类型: {report['overview']['release_type']}")
    print(f"    当前状态: {report['overview']['status']}")
    print(f"    校验通过率: {report['validation_analysis']['pass_rate']}%")
    print(f"    审批节点: {report['approval_analysis']['total_nodes']} 个")
    print(f"    熔断次数: {report['fuse_analysis']['total_fuse_events']} 次")

    print("\n" + "-" * 70)
    print("步骤 7: 审计日志完整性验证")
    print("-" * 70)

    integrity = orchestrator.audit_logger.verify_integrity()
    print(f"  审计记录总数: {integrity['total_records']}")
    print(f"  完整性校验: {'✓ 通过' if integrity['integrity_ok'] else '✗ 存在异常'}")
    if integrity["error_count"] > 0:
        print(f"  异常数量: {integrity['error_count']}")

    print("\n" + "=" * 70)
    print("  演示完成!")
    print("=" * 70)

    print("\n  生成的数据文件:")
    for root, dirs, files in os.walk(orchestrator.data_dir):
        for f in sorted(files):
            if f.endswith(".json") or f.endswith(".log") or f.endswith(".txt"):
                rel_path = os.path.relpath(os.path.join(root, f), orchestrator.data_dir)
                print(f"    {orchestrator.data_dir}/{rel_path}")

    return release_id


def main():
    parser = argparse.ArgumentParser(description="CTMS 系统版本发布与智能回滚自动化平台")
    parser.add_argument("--demo", action="store_true", help="运行完整流程演示")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--status", action="store_true", help="查看发布状态统计")
    parser.add_argument("--list", action="store_true", help="列出所有发布")
    parser.add_argument("--release-id", help="发布编号（用于查询/生成报告）")
    parser.add_argument("--report", action="store_true", help="生成发布复盘报告")
    parser.add_argument("--create", action="store_true", help="创建发布申请")
    parser.add_argument("--version", help="版本号")
    parser.add_argument("--project", help="项目编号")
    parser.add_argument("--title", help="发布标题")
    parser.add_argument("--applicant", help="申请人")
    parser.add_argument("--hotfix", action="store_true", help="是否紧急Hotfix")

    query_group = parser.add_argument_group("查询命令（需配合 --release-id 使用）")
    query_group.add_argument("--approval-status", action="store_true", help="查看审批节点状态")
    query_group.add_argument("--gray-status", action="store_true", help="查看灰度批次状态")
    query_group.add_argument("--metrics", action="store_true", help="查看最新监控指标")
    query_group.add_argument("--fuse-records", action="store_true", help="查看熔断记录")
    query_group.add_argument("--all-status", action="store_true", help="查看完整发布状态（审批+灰度+监控+熔断）")
    query_group.add_argument("--snapshot", action="store_true", help="导出进度快照（适合复制同步项目组）")
    query_group.add_argument("--timeline", action="store_true", help="查看合规时间线（串联关键操作流水）")

    audit_group = parser.add_argument_group("审计核验命令")
    audit_group.add_argument("--audit-verify", action="store_true", help="审计完整性核验（哈希链+时间线+操作统计）")
    audit_group.add_argument("--compare", nargs=2, metavar=("REL-A", "REL-B"),
                            help="多版本对比（传入两个发布编号）")
    audit_group.add_argument("--sync-package", action="store_true", help="导出项目同步包（Markdown + JSON双格式）")
    audit_group.add_argument("--sync-output-dir", help="同步包输出目录（默认 data/sync_packages/）")

    args = parser.parse_args()

    if args.demo:
        run_demo()
        return

    orchestrator = ReleaseOrchestrator(args.config)

    if args.status:
        summary = orchestrator.get_status_summary()
        print(f"总发布数: {summary['total']}")
        print("按状态分布:")
        for status, count in sorted(summary["by_status"].items()):
            print(f"  {status}: {count}")
        return

    if args.list:
        releases = orchestrator.list_releases()
        print(f"共 {len(releases)} 条发布记录:")
        print("")
        print("-" * 120)
        print(f"{'发布编号':<22} {'版本':<10} {'整体状态':<12} {'灰度阶段':<12} {'批次':<8} {'监控':<8} {'熔断':<6} {'审批':<8} 标题")
        print("-" * 120)
        for r in releases:
            stats = r.get("_stats", {})
            release_id = r["release_id"][:20] + ".." if len(r["release_id"]) > 22 else r["release_id"]
            version = r["version"][:10]
            status = r["status"][:12]
            gray_status = stats.get("gray_status", "N/A")[:12]
            batch_info = f"{stats.get('current_batch', 0)}/{stats.get('total_batches', 0)}"
            cycles = f"{stats.get('total_cycles', 0)}轮"
            fuses = f"{stats.get('total_fuse', 0)}次"
            approval = stats.get("approval_progress", "N/A")
            title = r["title"][:25] + ".." if len(r["title"]) > 25 else r["title"]
            print(f"{release_id:<22} {version:<10} {status:<12} {gray_status:<12} {batch_info:<8} {cycles:<8} {fuses:<6} {approval:<8} {title}")
        print("-" * 120)
        print("")
        print("图例: 整体状态(PENDING待审批/APPROVING审批中/GRAYING灰度中/COMPLETED完成/ROLLED_BACK回滚)")
        print("      灰度阶段(PLANNED已规划/RELEASING发布中/OBSERVING观察中/COMPLETED完成/ROLLED_BACK回滚)")
        return

    if args.create:
        if not all([args.version, args.project, args.title, args.applicant]):
            print("错误: 创建发布需要 --version, --project, --title, --applicant 参数")
            return
        release = orchestrator.create_release_request(
            version=args.version,
            project_id=args.project,
            title=args.title,
            description="",
            applicant=args.applicant,
            is_hotfix=args.hotfix
        )
        print(f"发布申请已创建: {release['release_id']}")
        return

    if args.release_id and args.report:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        report = orchestrator.generate_report(args.release_id)
        report_file = os.path.join(
            orchestrator.report_generator.report_dir,
            f"release_{args.release_id}_report.txt"
        )
        print(f"报告已生成: {report_file}")
        return

    if args.release_id and args.approval_status:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        print(orchestrator.get_approval_status_text(args.release_id))
        return

    if args.release_id and args.gray_status:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        print(orchestrator.get_gray_status_text(args.release_id))
        return

    if args.release_id and args.metrics:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        print(orchestrator.get_latest_metrics_text(args.release_id))
        return

    if args.release_id and args.fuse_records:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        print(orchestrator.get_fuse_records_text(args.release_id))
        return

    if args.release_id and args.snapshot:
        print(orchestrator.get_snapshot_text(args.release_id))
        return

    if args.release_id and args.timeline:
        print(orchestrator.get_timeline_text(args.release_id))
        return

    if args.release_id and args.sync_package:
        output_dir = getattr(args, "sync_output_dir", None) or None
        result = orchestrator.generate_sync_package(
            args.release_id, output_dir=output_dir
        )
        if result.get("success"):
            print("✅ 项目同步包已生成：")
            print(f"  Markdown: {result['markdown_path']}")
            print(f"  JSON:     {result['json_path']}")
            print("")
            print("（两份文件内容一致，可直接转发项目组或归档使用）")
        return

    if args.release_id and args.audit_verify:
        release = orchestrator.get_release(args.release_id)
        if not release:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
            return
        print(orchestrator.get_audit_verify_text(args.release_id))
        return

    if args.compare:
        rel_a, rel_b = args.compare
        print(orchestrator.get_compare_text(rel_a, rel_b))
        return

    if args.release_id and args.all_status:
        release = orchestrator.get_release(args.release_id)
        if release:
            print("=" * 60)
            print("  发布完整状态")
            print("=" * 60)
            print(f"发布编号: {release['release_id']}")
            print(f"版 本 号: {release['version']}")
            print(f"项目编号: {release['project_id']}")
            print(f"发布标题: {release['title']}")
            print(f"发布类型: {'紧急Hotfix' if release.get('is_hotfix') else '常规发布'}")
            print(f"申 请 人: {release['applicant']}")
            print(f"创建时间: {release['created_at']}")
            print(f"发布状态: {release['status']}")
            print(f"当前阶段: {release['current_stage']}")
            if release.get("gray_plan") and release["gray_plan"].get("status"):
                print(f"灰度阶段: {release['gray_plan']['status']}")
            print("")
            print(orchestrator.get_approval_status_text(args.release_id))
            print("")
            print(orchestrator.get_gray_status_text(args.release_id))
            print("")
            print(orchestrator.get_latest_metrics_text(args.release_id))
            print("")
            print(orchestrator.get_fuse_records_text(args.release_id))
        else:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
        return

    if args.release_id:
        release = orchestrator.get_release(args.release_id)
        if release:
            print(f"发布编号: {release['release_id']}")
            print(f"版 本 号: {release['version']}")
            print(f"发布状态: {release['status']}")
            print(f"当前阶段: {release['current_stage']}")
            if release.get("gray_plan") and release["gray_plan"].get("status"):
                print(f"灰度阶段: {release['gray_plan']['status']}")
        else:
            print(f"错误：发布编号不存在 - {args.release_id}")
            print(f"请使用 --list 命令查看所有发布")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
