#!/usr/bin/env python3
"""
CTMS 系统版本发布与智能回滚自动化平台 — 主入口
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, List

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

        report = self.report_generator.generate_release_report(
            release_id,
            release.get("validation_result"),
            release.get("approval_flow"),
            release.get("gray_plan")
        )
        return report

    def get_release(self, release_id: str) -> Dict[str, Any]:
        return self._load_release(release_id)

    def list_releases(self, status: str = None, project_id: str = None) -> List[Dict[str, Any]]:
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

            metric_count = len(batch.get("monitor_metrics", []))
            metric_by_name = {}
            for m in batch.get("monitor_metrics", []):
                name = m["metric_name"]
                if name not in metric_by_name or m["collected_at"] > metric_by_name[name]["collected_at"]:
                    metric_by_name[name] = m

            lines.append(f"    监控轮数: {len(metric_by_name) if metric_by_name else 0} 项指标 / 共 {metric_count} 条记录")

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
        for r in releases:
            print(f"  {r['release_id']} | {r['version']} | {r['status']} | {r['title']}")
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
        report = orchestrator.generate_report(args.release_id)
        report_file = os.path.join(
            orchestrator.report_generator.report_dir,
            f"release_{args.release_id}_report.txt"
        )
        print(f"报告已生成: {report_file}")
        return

    if args.release_id and args.approval_status:
        print(orchestrator.get_approval_status_text(args.release_id))
        return

    if args.release_id and args.gray_status:
        print(orchestrator.get_gray_status_text(args.release_id))
        return

    if args.release_id and args.metrics:
        print(orchestrator.get_latest_metrics_text(args.release_id))
        return

    if args.release_id and args.fuse_records:
        print(orchestrator.get_fuse_records_text(args.release_id))
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
            print("")
            print(orchestrator.get_approval_status_text(args.release_id))
            print("")
            print(orchestrator.get_gray_status_text(args.release_id))
            print("")
            print(orchestrator.get_latest_metrics_text(args.release_id))
            print("")
            print(orchestrator.get_fuse_records_text(args.release_id))
        else:
            print(f"发布不存在: {args.release_id}")
        return

    if args.release_id:
        release = orchestrator.get_release(args.release_id)
        if release:
            print(f"发布编号: {release['release_id']}")
            print(f"版 本 号: {release['version']}")
            print(f"发布状态: {release['status']}")
            print(f"当前阶段: {release['current_stage']}")
        else:
            print(f"发布不存在: {args.release_id}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
