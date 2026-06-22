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
    hours_between,
    ApprovalStatus,
    ReleaseType,
    ReleaseStatus
)
from modules.audit import AuditLogger


class ApprovalWorkflow:
    def __init__(self, config, audit_logger: AuditLogger = None):
        self.config = config
        self.audit_logger = audit_logger
        self.approval_config = config["approval"]
        self.data_dir = config["storage"]["data_dir"]
        self.approval_dir = os.path.join(self.data_dir, "approvals")
        ensure_dir(self.approval_dir)
        self.logger = setup_logger("approval", os.path.join(self.data_dir, "approval.log"))

    def identify_release_channel(self, version: str, is_hotfix: bool = False) -> str:
        if is_hotfix:
            return ReleaseType.HOTFIX
        parts = version.split(".")
        if len(parts) >= 3 and parts[2] != "0":
            return ReleaseType.HOTFIX
        return ReleaseType.NORMAL

    def create_approval_flow(self, release_id: str, version: str,
                             project_id: str, applicant: str,
                             is_hotfix: bool = False,
                             hotfix_reason: str = None,
                             custom_approvers: Dict[str, str] = None) -> Dict[str, Any]:
        release_type = self.identify_release_channel(version, is_hotfix)
        self.logger.info(f"创建审批流: release_id={release_id}, type={release_type}")

        flow = {
            "release_id": release_id,
            "version": version,
            "project_id": project_id,
            "release_type": release_type,
            "applicant": applicant,
            "created_at": get_now_iso(),
            "status": "IN_PROGRESS",
            "nodes": [],
            "hotfix_reason": hotfix_reason,
            "deviation_recorded": False
        }

        if release_type == ReleaseType.NORMAL:
            flow["nodes"] = self._create_normal_nodes(custom_approvers)
            flow["mode"] = "serial"
        else:
            flow["nodes"] = self._create_hotfix_nodes(custom_approvers)
            flow["mode"] = "parallel"

        self._save_approval_flow(flow)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="APPROVAL_FLOW_CREATED",
                operator=applicant,
                target_type="release",
                target_id=release_id,
                after_value={
                    "release_type": release_type,
                    "node_count": len(flow["nodes"]),
                    "mode": flow["mode"]
                },
                remark=f"创建{release_type}审批流，共{len(flow['nodes'])}个审批节点"
            )

        return flow

    def _create_normal_nodes(self, custom_approvers: Dict[str, str] = None) -> List[Dict[str, Any]]:
        nodes = []
        for node_cfg in self.approval_config["normal"]["nodes"]:
            approver = None
            if custom_approvers and node_cfg["name"] in custom_approvers:
                approver = custom_approvers[node_cfg["name"]]
            else:
                approver = self._get_default_approver(node_cfg["role"])

            node = {
                "order": node_cfg["order"],
                "name": node_cfg["name"],
                "label": node_cfg["label"],
                "role": node_cfg["role"],
                "approver": approver,
                "status": "NOT_STARTED",
                "timeout_hours": node_cfg["timeout_hours"],
                "comment": None,
                "approved_at": None,
                "delegated_to": None,
                "is_post_approval": False,
                "activated_at": None
            }
            nodes.append(node)

        nodes.sort(key=lambda x: x["order"])
        if nodes:
            nodes[0]["status"] = ApprovalStatus.PENDING
            nodes[0]["activated_at"] = get_now_iso()

        return nodes

    def _create_hotfix_nodes(self, custom_approvers: Dict[str, str] = None) -> List[Dict[str, Any]]:
        nodes = []
        hotfix_cfg = self.approval_config["hotfix"]
        for node_cfg in self.approval_config["normal"]["nodes"]:
            approver = None
            if custom_approvers and node_cfg["name"] in custom_approvers:
                approver = custom_approvers[node_cfg["name"]]
            else:
                approver = self._get_default_approver(node_cfg["role"])

            node = {
                "order": node_cfg["order"],
                "name": node_cfg["name"],
                "label": node_cfg["label"],
                "role": node_cfg["role"],
                "approver": approver,
                "status": ApprovalStatus.PENDING,
                "timeout_hours": hotfix_cfg["timeout_hours"],
                "comment": None,
                "approved_at": None,
                "delegated_to": None,
                "is_post_approval": False,
                "activated_at": get_now_iso()
            }
            nodes.append(node)

        return nodes

    def _get_default_approver(self, role: str) -> str:
        role_map = {
            "clinical_reviewer": "dr_zhang",
            "data_manager": "dm_li",
            "qa_specialist": "qa_wang",
            "project_manager": "pm_chen"
        }
        return role_map.get(role, f"user_{role}")

    def _get_current_pending_node(self, flow: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if flow["release_type"] == ReleaseType.NORMAL:
            for node in flow["nodes"]:
                if node["status"] == ApprovalStatus.PENDING:
                    return node
            return None
        else:
            pending = [n for n in flow["nodes"] if n["status"] == ApprovalStatus.PENDING]
            return pending[0] if pending else None

    def approve(self, release_id: str, node_name: str, approver: str,
                comment: str = None, is_post_approval: bool = False) -> Dict[str, Any]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            raise ValueError(f"审批流不存在: {release_id}")

        if flow["status"] == "APPROVED":
            raise ValueError("审批流已全部通过，无需重复审批")
        if flow["status"] == "REJECTED":
            raise ValueError("审批流已被驳回，无法继续审批")

        node = next((n for n in flow["nodes"] if n["name"] == node_name), None)
        if not node:
            raise ValueError(f"审批节点不存在: {node_name}")

        if flow["release_type"] == ReleaseType.NORMAL:
            if node["status"] == "NOT_STARTED":
                current_node = self._get_current_pending_node(flow)
                prev_nodes = [n for n in flow["nodes"] if n["order"] < node["order"]]
                pending_prev = [n for n in prev_nodes
                                if n["status"] not in [ApprovalStatus.APPROVED, ApprovalStatus.POST_APPROVED]]
                msg = (
                    f"无法审批【{node['label']}】节点："
                    f"当前轮不到该节点处理。\n"
                    f"  当前待审批节点：{current_node['label'] if current_node else '无'}（审批人：{current_node['approver'] if current_node else 'N/A'}）\n"
                    f"  尚未通过的前置节点：{', '.join([n['label'] for n in pending_prev]) if pending_prev else '无'}\n"
                    f"  请按顺序完成：临床审批 → 数据审批 → 质控审批 → PM审批"
                )
                raise PermissionError(msg)

        if node["status"] == ApprovalStatus.APPROVED:
            raise ValueError(f"节点【{node['label']}】已审批通过，无需重复审批")
        if node["status"] == ApprovalStatus.REJECTED:
            raise ValueError(f"节点【{node['label']}】已被驳回，无法审批")

        if node["status"] != ApprovalStatus.PENDING:
            raise ValueError(f"节点【{node['label']}】当前状态为 {node['status']}，不允许审批")

        old_status = node["status"]
        node["status"] = ApprovalStatus.POST_APPROVED if is_post_approval else ApprovalStatus.APPROVED
        node["approver"] = approver
        node["comment"] = comment
        node["approved_at"] = get_now_iso()
        node["is_post_approval"] = is_post_approval

        if flow["release_type"] == ReleaseType.NORMAL:
            current_idx = next(i for i, n in enumerate(flow["nodes"]) if n["name"] == node_name)
            if current_idx + 1 < len(flow["nodes"]):
                next_node = flow["nodes"][current_idx + 1]
                next_node["status"] = ApprovalStatus.PENDING
                next_node["activated_at"] = get_now_iso()

        flow = self._check_flow_complete(flow)
        self._save_approval_flow(flow)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="APPROVAL_NODE_APPROVED",
                operator=approver,
                target_type="release",
                target_id=release_id,
                before_value={"node": node_name, "status": old_status},
                after_value={"node": node_name, "status": node["status"]},
                remark=f"审批通过: {node['label']}"
            )

        return flow

    def reject(self, release_id: str, node_name: str, approver: str,
               reject_reason: str) -> Dict[str, Any]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            raise ValueError(f"审批流不存在: {release_id}")

        if flow["status"] == "APPROVED":
            raise ValueError("审批流已全部通过，无法驳回")
        if flow["status"] == "REJECTED":
            raise ValueError("审批流已被驳回，无需重复操作")

        node = next((n for n in flow["nodes"] if n["name"] == node_name), None)
        if not node:
            raise ValueError(f"审批节点不存在: {node_name}")

        if flow["release_type"] == ReleaseType.NORMAL:
            if node["status"] == "NOT_STARTED":
                current_node = self._get_current_pending_node(flow)
                msg = (
                    f"无法驳回【{node['label']}】节点："
                    f"当前轮不到该节点处理。\n"
                    f"  当前待审批节点：{current_node['label'] if current_node else '无'}（审批人：{current_node['approver'] if current_node else 'N/A'}）\n"
                    f"  请按顺序完成：临床审批 → 数据审批 → 质控审批 → PM审批"
                )
                raise PermissionError(msg)

        if node["status"] != ApprovalStatus.PENDING:
            raise ValueError(f"节点【{node['label']}】当前状态为 {node['status']}，无法驳回")

        old_status = node["status"]
        node["status"] = ApprovalStatus.REJECTED
        node["approver"] = approver
        node["comment"] = reject_reason
        node["approved_at"] = get_now_iso()

        flow["status"] = "REJECTED"
        flow["rejected_at"] = get_now_iso()
        flow["reject_reason"] = reject_reason

        self._save_approval_flow(flow)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="APPROVAL_NODE_REJECTED",
                operator=approver,
                target_type="release",
                target_id=release_id,
                before_value={"node": node_name, "status": old_status},
                after_value={"node": node_name, "status": ApprovalStatus.REJECTED},
                remark=f"审批驳回: {node['label']}, 原因: {reject_reason}"
            )

        return flow

    def delegate(self, release_id: str, node_name: str, from_approver: str,
                 to_approver: str, reason: str = None) -> Dict[str, Any]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            raise ValueError(f"审批流不存在: {release_id}")

        node = next((n for n in flow["nodes"] if n["name"] == node_name), None)
        if not node:
            raise ValueError(f"审批节点不存在: {node_name}")

        old_approver = node["approver"]
        node["delegated_to"] = to_approver
        node["approver"] = to_approver
        node["status"] = ApprovalStatus.DELEGATED
        node["delegation_reason"] = reason
        node["delegated_at"] = get_now_iso()

        self._save_approval_flow(flow)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="APPROVAL_DELEGATED",
                operator=from_approver,
                target_type="release",
                target_id=release_id,
                before_value={"node": node_name, "approver": old_approver},
                after_value={"node": node_name, "approver": to_approver},
                remark=f"审批转派: {node['label']} 从 {from_approver} 转至 {to_approver}"
            )

        return flow

    def _check_flow_complete(self, flow: Dict[str, Any]) -> Dict[str, Any]:
        all_approved = all(
            n["status"] in [ApprovalStatus.APPROVED, ApprovalStatus.POST_APPROVED]
            for n in flow["nodes"]
        )

        if all_approved:
            flow["status"] = "APPROVED"
            flow["approved_at"] = get_now_iso()

            has_post = any(n.get("is_post_approval") for n in flow["nodes"])
            if has_post:
                flow["deviation_recorded"] = True

        return flow

    def check_timeout(self, release_id: str) -> List[Dict[str, Any]]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            return []

        timeout_nodes = []
        now = datetime.now()

        for node in flow["nodes"]:
            if node["status"] == ApprovalStatus.PENDING and node.get("activated_at"):
                activated_at = datetime.fromisoformat(node["activated_at"])
                elapsed_hours = (now - activated_at).total_seconds() / 3600

                if elapsed_hours >= node["timeout_hours"]:
                    node["timeout"] = True
                    timeout_nodes.append({
                        "node_name": node["name"],
                        "node_label": node["label"],
                        "approver": node["approver"],
                        "timeout_hours": node["timeout_hours"],
                        "elapsed_hours": round(elapsed_hours, 2)
                    })
                elif elapsed_hours >= node["timeout_hours"] * 0.5:
                    node["reminder_needed"] = True

        self._save_approval_flow(flow)
        return timeout_nodes

    def get_approval_flow(self, release_id: str) -> Optional[Dict[str, Any]]:
        return self._load_approval_flow(release_id)

    def get_current_node(self, release_id: str) -> Optional[Dict[str, Any]]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            return None

        if flow["release_type"] == ReleaseType.NORMAL:
            for node in flow["nodes"]:
                if node["status"] == ApprovalStatus.PENDING:
                    return node
            return None
        else:
            pending = [n for n in flow["nodes"] if n["status"] == ApprovalStatus.PENDING]
            return pending if pending else None

    def get_approval_summary(self, release_id: str) -> Dict[str, Any]:
        flow = self._load_approval_flow(release_id)
        if not flow:
            return {}

        total = len(flow["nodes"])
        approved = sum(1 for n in flow["nodes"]
                       if n["status"] in [ApprovalStatus.APPROVED, ApprovalStatus.POST_APPROVED])
        rejected = sum(1 for n in flow["nodes"] if n["status"] == ApprovalStatus.REJECTED)
        pending = sum(1 for n in flow["nodes"] if n["status"] == ApprovalStatus.PENDING)

        total_hours = 0
        for node in flow["nodes"]:
            if node.get("approved_at") and node.get("activated_at"):
                approved_at = datetime.fromisoformat(node["approved_at"])
                activated_at = datetime.fromisoformat(node["activated_at"])
                total_hours += (approved_at - activated_at).total_seconds() / 3600

        return {
            "release_id": release_id,
            "release_type": flow["release_type"],
            "status": flow["status"],
            "total_nodes": total,
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "avg_approval_hours": round(total_hours / approved, 2) if approved > 0 else 0
        }

    def _load_approval_flow(self, release_id: str) -> Optional[Dict[str, Any]]:
        file_path = os.path.join(self.approval_dir, f"{release_id}.json")
        if os.path.exists(file_path):
            return read_json_file(file_path)
        return None

    def _save_approval_flow(self, flow: Dict[str, Any]):
        file_path = os.path.join(self.approval_dir, f"{flow['release_id']}.json")
        write_json_file(file_path, flow)

    def generate_approval_report(self, release_id: str) -> str:
        flow = self._load_approval_flow(release_id)
        if not flow:
            return "审批流不存在"

        lines = []
        lines.append("=" * 60)
        lines.append("  审批流程报告")
        lines.append("=" * 60)
        lines.append(f"发布编号: {flow['release_id']}")
        lines.append(f"版 本 号: {flow['version']}")
        lines.append(f"发布类型: {'常规发布' if flow['release_type'] == ReleaseType.NORMAL else '紧急Hotfix'}")
        lines.append(f"审批模式: {'串行审批' if flow['mode'] == 'serial' else '并行审批'}")
        lines.append(f"申 请 人: {flow['applicant']}")
        lines.append(f"创建时间: {flow['created_at']}")
        lines.append(f"当前状态: {flow['status']}")
        lines.append("")

        lines.append("【审批节点】")
        current_node = self._get_current_pending_node(flow)
        if current_node and flow["status"] == "IN_PROGRESS":
            lines.append(f"  >>> 当前待审批：{current_node['label']}（{current_node['approver']}）<<<")
            lines.append("")

        for i, node in enumerate(flow["nodes"], 1):
            status_map = {
                ApprovalStatus.PENDING: "待审批",
                ApprovalStatus.APPROVED: "已通过",
                ApprovalStatus.REJECTED: "已驳回",
                ApprovalStatus.DELEGATED: "已转派",
                ApprovalStatus.POST_APPROVED: "事后补签",
                "NOT_STARTED": "未开始"
            }
            status_str = status_map.get(node["status"], node["status"])

            prefix = ""
            if node["status"] == ApprovalStatus.PENDING:
                prefix = "▶ "
            elif node["status"] == ApprovalStatus.APPROVED or node["status"] == ApprovalStatus.POST_APPROVED:
                prefix = "✓ "
            elif node["status"] == ApprovalStatus.REJECTED:
                prefix = "✗ "
            else:
                prefix = "○ "

            lines.append(f"  {prefix}{i}. {node['label']} - {node['approver']} - {status_str}")
            if node.get("approved_at"):
                lines.append(f"     审批时间: {node['approved_at']}")
            if node.get("comment"):
                lines.append(f"     审批意见: {node['comment']}")
            if node.get("is_post_approval"):
                lines.append(f"     备注: 事后补签")

        if flow.get("hotfix_reason"):
            lines.append("")
            lines.append("【紧急发布原因】")
            lines.append(f"  {flow['hotfix_reason']}")

        if flow.get("reject_reason"):
            lines.append("")
            lines.append("【驳回原因】")
            lines.append(f"  {flow['reject_reason']}")

        return "\n".join(lines)
