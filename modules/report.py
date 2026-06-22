import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from common.utils import (
    setup_logger,
    get_now_iso,
    get_now_str,
    ensure_dir,
    write_json_file,
    read_json_file,
    json_dumps,
    hours_between,
    ReleaseStatus,
    ReleaseType
)
from modules.audit import AuditLogger


class ReportGenerator:
    def __init__(self, config, audit_logger: AuditLogger = None):
        self.config = config
        self.audit_logger = audit_logger
        self.data_dir = config["storage"]["data_dir"]
        self.report_dir = config["storage"]["report_dir"]
        self.release_dir = config["storage"]["release_dir"]
        self.validation_dir = os.path.join(self.data_dir, "validations")
        self.approval_dir = os.path.join(self.data_dir, "approvals")
        self.gray_dir = os.path.join(self.data_dir, "gray_release")
        ensure_dir(self.report_dir)
        ensure_dir(self.release_dir)
        self.logger = setup_logger("report", os.path.join(self.data_dir, "report.log"))

    def generate_release_report(self, release_id: str,
                                validation_result: Dict[str, Any] = None,
                                approval_flow: Dict[str, Any] = None,
                                gray_plan: Dict[str, Any] = None) -> Dict[str, Any]:
        self.logger.info(f"生成发布复盘报告: release_id={release_id}")

        if validation_result is None:
            validation_result = self._load_validation_result(release_id)
        if approval_flow is None:
            approval_flow = self._load_approval_flow(release_id)
        if gray_plan is None:
            gray_plan = self._load_gray_plan(release_id)

        report = {
            "report_type": "release_review",
            "release_id": release_id,
            "generated_at": get_now_iso(),
            "overview": self._build_overview(validation_result, approval_flow, gray_plan),
            "validation_analysis": self._build_validation_analysis(validation_result),
            "approval_analysis": self._build_approval_analysis(approval_flow),
            "gray_analysis": self._build_gray_analysis(gray_plan),
            "fuse_analysis": self._build_fuse_analysis(gray_plan),
            "risks_and_improvements": self._build_risks_and_improvements(
                validation_result, approval_flow, gray_plan
            ),
            "appendix": {
                "audit_trail": self._get_audit_summary(release_id),
                "timeline": self._build_timeline(validation_result, approval_flow, gray_plan)
            }
        }

        report_file = os.path.join(self.report_dir, f"release_{release_id}_report.json")
        write_json_file(report_file, report)

        text_report = self._format_text_report(report)
        text_file = os.path.join(self.report_dir, f"release_{release_id}_report.txt")
        with open(text_file, "w", encoding="utf-8") as f:
            f.write(text_report)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="RELEASE_REPORT_GENERATED",
                operator="system",
                target_type="release",
                target_id=release_id,
                after_value={"report_file": report_file},
                remark="发布复盘报告生成完成"
            )

        return report

    def _build_overview(self, validation_result, approval_flow, gray_plan) -> Dict[str, Any]:
        release_type = "常规发布"
        version = "N/A"
        applicant = "N/A"
        apply_time = None

        if approval_flow:
            release_type = "常规发布" if approval_flow.get("release_type") == ReleaseType.NORMAL else "紧急Hotfix"
            version = approval_flow.get("version", "N/A")
            applicant = approval_flow.get("applicant", "N/A")
            apply_time = approval_flow.get("created_at", None)
        elif validation_result:
            version = validation_result.get("version", "N/A")

        status = "进行中"
        if gray_plan:
            status_map = {
                "PLANNED": "已规划",
                "RELEASING": "发布中",
                "OBSERVING": "观察中",
                "ALL_COMPLETED": "已完成",
                "FAILED": "发布失败",
                "PARTIAL_ROLLBACK": "部分回滚",
                "FULL_ROLLBACK": "全量回滚"
            }
            status = status_map.get(gray_plan.get("status"), gray_plan.get("status", "进行中"))
        elif approval_flow:
            status_map = {
                "IN_PROGRESS": "审批中",
                "APPROVED": "审批通过",
                "REJECTED": "审批驳回"
            }
            status = status_map.get(approval_flow.get("status"), "审批中")
        elif validation_result:
            status = "校验阻断" if validation_result["summary"]["blocked"] else "校验通过"

        total_duration_hours = 0
        if apply_time and gray_plan and gray_plan.get("status") in ["ALL_COMPLETED", "FULL_ROLLBACK"]:
            end_time = None
            for batch in gray_plan.get("batches", []):
                if batch.get("rollback_time"):
                    end_time = batch["rollback_time"]
                elif batch.get("release_time"):
                    end_time = batch["release_time"]
            if end_time:
                total_duration_hours = hours_between(apply_time, end_time)

        return {
            "version": version,
            "release_type": release_type,
            "applicant": applicant,
            "apply_time": apply_time,
            "status": status,
            "total_duration_hours": round(total_duration_hours, 2),
            "summary": f"{release_type} - {status}"
        }

    def _build_validation_analysis(self, validation_result) -> Dict[str, Any]:
        if not validation_result:
            return {"available": False}

        summary = validation_result["summary"]
        dimensions = validation_result["dimensions"]

        dim_stats = {}
        for dim_key, dim_data in dimensions.items():
            dim_stats[dim_key] = {
                "label": dim_data["label"],
                "passed": dim_data["passed_count"],
                "total": dim_data["total_count"],
                "pass_rate": round(dim_data["passed_count"] / dim_data["total_count"] * 100, 2) if dim_data["total_count"] > 0 else 0
            }

        failed_items = []
        for dim_key, dim_data in dimensions.items():
            for check in dim_data["checks"]:
                if check["result"] != "PASS":
                    failed_items.append({
                        "dimension": dim_data["label"],
                        "item": check["label"],
                        "result": check["result"],
                        "severity": check["severity"],
                        "blocking": check["blocking"],
                        "suggestion": check.get("suggestion")
                    })

        return {
            "available": True,
            "total": summary["total"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "warnings": summary["warnings"],
            "pass_rate": round(summary["passed"] / summary["total"] * 100, 2) if summary["total"] > 0 else 0,
            "blocked": summary["blocked"],
            "dimensions": dim_stats,
            "failed_items": failed_items
        }

    def _build_approval_analysis(self, approval_flow) -> Dict[str, Any]:
        if not approval_flow:
            return {"available": False}

        nodes = approval_flow.get("nodes", [])
        total = len(nodes)
        approved = sum(1 for n in nodes if n["status"] in ["APPROVED", "POST_APPROVED"])
        rejected = sum(1 for n in nodes if n["status"] == "REJECTED")
        pending = sum(1 for n in nodes if n["status"] == "PENDING")

        node_details = []
        total_approval_hours = 0
        for node in nodes:
            duration_hours = 0
            if node.get("approved_at") and node.get("activated_at"):
                duration_hours = hours_between(node["activated_at"], node["approved_at"])
                total_approval_hours += duration_hours

            node_details.append({
                "name": node["label"],
                "approver": node["approver"],
                "status": node["status"],
                "duration_hours": round(duration_hours, 2),
                "comment": node.get("comment"),
                "is_post_approval": node.get("is_post_approval", False)
            })

        avg_hours = round(total_approval_hours / approved, 2) if approved > 0 else 0

        return {
            "available": True,
            "mode": "串行" if approval_flow.get("mode") == "serial" else "并行",
            "total_nodes": total,
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "avg_approval_hours": avg_hours,
            "nodes": node_details,
            "has_deviation": approval_flow.get("deviation_recorded", False),
            "hotfix_reason": approval_flow.get("hotfix_reason")
        }

    def _build_gray_analysis(self, gray_plan) -> Dict[str, Any]:
        if not gray_plan:
            return {"available": False}

        batches = gray_plan.get("batches", [])
        total_centers = gray_plan.get("total_centers", 0)

        batch_details = []
        release_duration_hours = 0

        for batch in batches:
            batch_info = {
                "batch_no": batch["batch_no"],
                "level": batch["label"],
                "center_count": batch["center_count"],
                "status": batch["status"],
                "observation_hours": batch["observation_hours"],
                "release_time": batch.get("release_time"),
                "rollback_time": batch.get("rollback_time"),
                "metric_count": len(batch.get("monitor_metrics", [])),
                "fuse_event_count": len(batch.get("fuse_events", []))
            }

            if batch.get("release_time") and batch.get("observation_start_time"):
                duration = hours_between(batch["release_time"], batch["observation_start_time"])
                batch_info["release_duration_minutes"] = round(duration * 60, 2)

            batch_details.append(batch_info)

        return {
            "available": True,
            "total_centers": total_centers,
            "batch_count": len(batches),
            "status": gray_plan.get("status"),
            "batches": batch_details
        }

    def _build_fuse_analysis(self, gray_plan) -> Dict[str, Any]:
        if not gray_plan:
            return {"available": False}

        all_fuse_events = []
        total_fuse_events = 0

        for batch in gray_plan.get("batches", []):
            for event in batch.get("fuse_events", []):
                total_fuse_events += 1
                all_fuse_events.append({
                    "batch_no": batch["batch_no"],
                    "fuse_level": event["fuse_level"],
                    "fuse_level_name": event["fuse_level_name"],
                    "trigger_time": event["trigger_time"],
                    "reasons": event.get("reasons", [])
                })

        return {
            "available": True,
            "total_fuse_events": total_fuse_events,
            "events": all_fuse_events,
            "had_fuse": total_fuse_events > 0
        }

    def _build_risks_and_improvements(self, validation_result, approval_flow, gray_plan) -> Dict[str, Any]:
        risks = []
        improvements = []

        if validation_result and validation_result["summary"]["blocked"]:
            risks.append({
                "type": "校验阻断",
                "description": f"发布被前置校验阻断，共 {len(validation_result['summary']['blocking_items'])} 项高危问题",
                "severity": "高"
            })
            improvements.append("优化发布前自测流程，减少阻断项")

        if approval_flow:
            post_approval_count = sum(1 for n in approval_flow.get("nodes", [])
                                      if n.get("is_post_approval"))
            if post_approval_count > 0:
                risks.append({
                    "type": "事后补签",
                    "description": f"存在 {post_approval_count} 个审批节点为事后补签，属于合规偏差",
                    "severity": "中"
                })
                improvements.append("优化紧急发布审批效率，减少事后补签")

        if gray_plan:
            total_fuse = sum(len(b.get("fuse_events", [])) for b in gray_plan.get("batches", []))
            if total_fuse > 0:
                risks.append({
                    "type": "熔断触发",
                    "description": f"灰度发布期间触发 {total_fuse} 次熔断",
                    "severity": "高"
                })
                improvements.append("加强发布前测试覆盖，降低生产环境故障概率")

        if not risks:
            risks.append({
                "type": "无",
                "description": "本次发布未发现显著风险",
                "severity": "低"
            })

        if not improvements:
            improvements.append("发布过程顺利，持续保持")

        return {
            "risks": risks,
            "improvements": improvements
        }

    def _build_timeline(self, validation_result, approval_flow, gray_plan) -> List[Dict[str, Any]]:
        timeline = []

        if approval_flow:
            timeline.append({
                "time": approval_flow.get("created_at"),
                "event": "发布申请提交",
                "operator": approval_flow.get("applicant", "N/A")
            })

        if validation_result:
            timeline.append({
                "time": validation_result.get("checked_at"),
                "event": "前置校验完成",
                "operator": "system",
                "detail": f"{'通过' if not validation_result['summary']['blocked'] else '阻断'}"
            })

        if approval_flow:
            for node in approval_flow.get("nodes", []):
                if node.get("approved_at"):
                    timeline.append({
                        "time": node["approved_at"],
                        "event": f"{node['label']} {'通过' if node['status'] == 'APPROVED' else '驳回'}",
                        "operator": node.get("approver")
                    })

        if gray_plan:
            for batch in gray_plan.get("batches", []):
                if batch.get("release_time"):
                    timeline.append({
                        "time": batch["release_time"],
                        "event": f"第{batch['batch_no']}批发布开始",
                        "operator": "system"
                    })
                if batch.get("rollback_time"):
                    timeline.append({
                        "time": batch["rollback_time"],
                        "event": f"第{batch['batch_no']}批回滚",
                        "operator": "system"
                    })

        timeline.sort(key=lambda x: x.get("time", ""))
        return timeline

    def _get_audit_summary(self, release_id: str) -> Dict[str, Any]:
        if not self.audit_logger:
            return {"available": False}

        logs = self.audit_logger.query(target_type="release", target_id=release_id)
        op_types = {}
        for log in logs:
            op = log.get("operation_type", "unknown")
            op_types[op] = op_types.get(op, 0) + 1

        return {
            "available": True,
            "total_logs": len(logs),
            "operation_types": op_types
        }

    def generate_daily_report(self, date_str: str = None) -> Dict[str, Any]:
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        self.logger.info(f"生成日报表: {date_str}")

        report = {
            "report_type": "daily",
            "date": date_str,
            "generated_at": get_now_iso(),
            "release_stats": self._get_daily_release_stats(date_str),
            "validation_stats": self._get_daily_validation_stats(date_str),
            "approval_stats": self._get_daily_approval_stats(date_str),
            "gray_stats": self._get_daily_gray_stats(date_str)
        }

        report_file = os.path.join(self.report_dir, f"daily_{date_str}.json")
        write_json_file(report_file, report)

        return report

    def _get_daily_release_stats(self, date_str: str) -> Dict[str, Any]:
        release_files = []
        if os.path.exists(self.release_dir):
            for f in os.listdir(self.release_dir):
                if f.endswith(".json") and date_str in f:
                    release_files.append(f)

        return {
            "total_applications": 0,
            "completed": 0,
            "blocked": 0,
            "rollbacks": 0
        }

    def _get_daily_validation_stats(self, date_str: str) -> Dict[str, Any]:
        return {
            "total_validations": 0,
            "pass_rate": 0,
            "block_rate": 0
        }

    def _get_daily_approval_stats(self, date_str: str) -> Dict[str, Any]:
        return {
            "total_approvals": 0,
            "avg_approval_hours": 0,
            "timeout_count": 0
        }

    def _get_daily_gray_stats(self, date_str: str) -> Dict[str, Any]:
        return {
            "total_gray_releases": 0,
            "success_rate": 0,
            "fuse_count": 0
        }

    def generate_trend_report(self, start_date: str, end_date: str) -> Dict[str, Any]:
        self.logger.info(f"生成趋势报表: {start_date} ~ {end_date}")

        report = {
            "report_type": "trend",
            "start_date": start_date,
            "end_date": end_date,
            "generated_at": get_now_iso(),
            "release_frequency": {"total": 0, "normal": 0, "hotfix": 0},
            "validation_trend": {"pass_rate_trend": [], "block_rate_trend": []},
            "approval_trend": {"avg_hours_trend": [], "timeout_rate_trend": []},
            "gray_trend": {"success_rate_trend": [], "fuse_rate_trend": []},
            "quality_score": 0
        }

        return report

    def _format_text_report(self, report: Dict[str, Any]) -> str:
        lines = []
        lines.append("=" * 70)
        lines.append("  CTMS 系统发布复盘报告")
        lines.append("=" * 70)
        lines.append("")

        overview = report["overview"]
        lines.append("【一、发布概览】")
        lines.append(f"  版本号: {overview['version']}")
        lines.append(f"  发布类型: {overview['release_type']}")
        lines.append(f"  申请人: {overview['applicant']}")
        lines.append(f"  申请时间: {overview.get('apply_time', 'N/A')}")
        lines.append(f"  当前状态: {overview['status']}")
        lines.append(f"  总耗时: {overview['total_duration_hours']} 小时")
        lines.append("")

        val = report["validation_analysis"]
        if val.get("available"):
            lines.append("【二、前置校验分析】")
            lines.append(f"  总校验项: {val['total']}")
            lines.append(f"  通过: {val['passed']}  ({val['pass_rate']}%)")
            lines.append(f"  失败: {val['failed']}")
            lines.append(f"  警告: {val['warnings']}")
            lines.append(f"  是否阻断: {'是' if val['blocked'] else '否'}")
            lines.append("")
            lines.append("  各维度通过率:")
            for dim_key, dim_stat in val["dimensions"].items():
                lines.append(f"    - {dim_stat['label']}: {dim_stat['passed']}/{dim_stat['total']} ({dim_stat['pass_rate']}%)")
            if val["failed_items"]:
                lines.append("")
                lines.append("  未通过项:")
                for i, item in enumerate(val["failed_items"], 1):
                    lines.append(f"    {i}. [{item['severity']}] {item['item']} - {item['dimension']}")
                    if item.get("suggestion"):
                        lines.append(f"       建议: {item['suggestion']}")
            lines.append("")

        appr = report["approval_analysis"]
        if appr.get("available"):
            lines.append("【三、审批效率分析】")
            lines.append(f"  审批模式: {appr['mode']}审批")
            lines.append(f"  审批节点: {appr['total_nodes']} 个")
            lines.append(f"  已通过: {appr['approved']}")
            lines.append(f"  已驳回: {appr['rejected']}")
            lines.append(f"  待审批: {appr['pending']}")
            lines.append(f"  平均审批时长: {appr['avg_approval_hours']} 小时")
            if appr.get("has_deviation"):
                lines.append(f"  合规偏差: 是 (存在事后补签)")
            lines.append("")
            lines.append("  各节点详情:")
            for i, node in enumerate(appr["nodes"], 1):
                status_map = {
                    "PENDING": "待审批",
                    "APPROVED": "已通过",
                    "REJECTED": "已驳回",
                    "DELEGATED": "已转派",
                    "POST_APPROVED": "事后补签"
                }
                status_str = status_map.get(node["status"], node["status"])
                lines.append(f"    {i}. {node['name']} - {node['approver']} - {status_str}")
                if node["duration_hours"] > 0:
                    lines.append(f"       耗时: {node['duration_hours']} 小时")
            if appr.get("hotfix_reason"):
                lines.append("")
                lines.append(f"  紧急发布原因: {appr['hotfix_reason']}")
            lines.append("")

        gray = report["gray_analysis"]
        if gray.get("available"):
            lines.append("【四、灰度发布分析】")
            lines.append(f"  中心总数: {gray['total_centers']} 个")
            lines.append(f"  批次数: {gray['batch_count']}")
            lines.append(f"  发布状态: {gray['status']}")
            lines.append("")
            lines.append("  各批次详情:")
            for batch in gray["batches"]:
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

                lines.append(f"  {prefix}第{batch['batch_no']}批 ({batch['level']}): {batch['center_count']}个中心 - {status_str}")
                if batch.get("release_time"):
                    lines.append(f"      发布时间: {batch['release_time']}")
                if batch.get("rollback_time"):
                    lines.append(f"      回滚时间: {batch['rollback_time']}")
                lines.append(f"      监控采集: {batch.get('metric_count', 0)} 轮")
                if batch.get("fuse_event_count", 0) > 0:
                    lines.append(f"      熔断事件: {batch['fuse_event_count']} 次")
                if batch.get("latest_metrics"):
                    lines.append(f"      最新监控指标:")
                    for m in batch["latest_metrics"]:
                        icon = "✓" if m["status"] == "NORMAL" else "⚠" if m["status"] == "WARN" else "✗"
                        lines.append(f"        {icon} {m['label']}: {m['value']}{m.get('unit', '')}")
            lines.append("")

        fuse = report["fuse_analysis"]
        if fuse.get("available") and fuse.get("had_fuse"):
            lines.append("【五、熔断回滚分析】")
            lines.append(f"  熔断总次数: {fuse['total_fuse_events']}")
            lines.append("")
            for event in fuse["events"]:
                lines.append(f"  - 批次 {event['batch_no']}: {event['fuse_level_name']}")
                lines.append(f"    触发时间: {event['trigger_time']}")
                for reason in event.get("reasons", []):
                    lines.append(f"    原因: {reason['metric']} = {reason['value']}{reason.get('unit', '')} (阈值: {reason['threshold']})")
            lines.append("")

        risks = report["risks_and_improvements"]
        lines.append("【六、风险与改进建议】")
        lines.append("  风险点:")
        for i, risk in enumerate(risks["risks"], 1):
            lines.append(f"    {i}. [{risk['severity']}] {risk['type']}: {risk['description']}")
        lines.append("")
        lines.append("  改进建议:")
        for i, imp in enumerate(risks["improvements"], 1):
            lines.append(f"    {i}. {imp}")
        lines.append("")

        appendix = report["appendix"]
        lines.append("【七、附录 - 关键时间线】")
        for event in appendix["timeline"]:
            lines.append(f"  {event['time']} | {event['event']} | {event['operator']}")

        lines.append("")
        lines.append("=" * 70)
        lines.append(f"  报告生成时间: {report['generated_at']}")
        lines.append("=" * 70)

        return "\n".join(lines)

    def _load_validation_result(self, release_id: str):
        file_path = os.path.join(self.validation_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None

    def _load_approval_flow(self, release_id: str):
        file_path = os.path.join(self.approval_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None

    def _load_gray_plan(self, release_id: str):
        file_path = os.path.join(self.gray_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None
