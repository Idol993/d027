import os
from datetime import datetime, timedelta
from typing import List, Dict, Any

from common.utils import (
    setup_logger,
    get_now_str,
    get_now_iso,
    ensure_dir,
    write_json_file,
    read_json_file,
    json_dumps,
    ValidationResult,
    Severity,
    ReleaseStatus
)
from modules.audit import AuditLogger


class PreValidator:
    def __init__(self, config, audit_logger: AuditLogger = None):
        self.config = config
        self.audit_logger = audit_logger
        self.val_config = config["validation"]
        self.data_dir = config["storage"]["data_dir"]
        self.validation_dir = os.path.join(self.data_dir, "validations")
        ensure_dir(self.validation_dir)
        self.logger = setup_logger("pre_validation", os.path.join(self.data_dir, "pre_validation.log"))

    def validate_all(self, release_id: str, project_id: str, version: str,
                     mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        self.logger.info(f"开始全量前置校验: release_id={release_id}, project_id={project_id}, version={version}")

        results = {
            "release_id": release_id,
            "project_id": project_id,
            "version": version,
            "checked_at": get_now_iso(),
            "dimensions": {},
            "summary": {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "warnings": 0,
                "blocked": False,
                "blocking_items": []
            }
        }

        subject_result = self.validate_subject_data(release_id, project_id, mock_data)
        results["dimensions"]["subject_data"] = subject_result

        ethics_result = self.validate_ethics_compliance(release_id, project_id, mock_data)
        results["dimensions"]["ethics_compliance"] = ethics_result

        progress_result = self.validate_trial_progress(release_id, project_id, mock_data)
        results["dimensions"]["trial_progress"] = progress_result

        document_result = self.validate_electronic_documents(release_id, project_id, mock_data)
        results["dimensions"]["electronic_documents"] = document_result

        all_checks = (subject_result["checks"] + ethics_result["checks"] +
                      progress_result["checks"] + document_result["checks"])

        results["summary"]["total"] = len(all_checks)
        results["summary"]["passed"] = sum(1 for c in all_checks if c["result"] == ValidationResult.PASS)
        results["summary"]["failed"] = sum(1 for c in all_checks if c["result"] == ValidationResult.FAIL)
        results["summary"]["warnings"] = sum(1 for c in all_checks if c["result"] == ValidationResult.WARN)

        blocking_items = [c for c in all_checks
                          if c["result"] == ValidationResult.FAIL and c["severity"] == Severity.HIGH]
        results["summary"]["blocking_items"] = blocking_items
        results["summary"]["blocked"] = len(blocking_items) > 0

        result_file = os.path.join(self.validation_dir, f"{release_id}.json")
        write_json_file(result_file, results)

        if self.audit_logger:
            self.audit_logger.log(
                operation_type="VALIDATION_COMPLETED",
                operator="system",
                target_type="release",
                target_id=release_id,
                after_value={
                    "passed": results["summary"]["passed"],
                    "failed": results["summary"]["failed"],
                    "blocked": results["summary"]["blocked"]
                },
                remark=f"前置校验完成：{'阻断' if results['summary']['blocked'] else '通过'}"
            )

        if results["summary"]["blocked"]:
            self.logger.warning(f"前置校验阻断: 阻断发布，共 {len(blocking_items)} 项高危未通过")
        else:
            self.logger.info(f"前置校验通过: {results['summary']['passed']}/{results['summary']['total']} 项通过")

        return results

    def validate_subject_data(self, release_id: str, project_id: str,
                              mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        checks = []
        vc = self.val_config

        checks.append(self._check_edc_sync_status(mock_data, vc))
        checks.append(self._check_subject_field_consistency(mock_data, vc))
        checks.append(self._check_open_queries(mock_data, vc))

        passed = sum(1 for c in checks if c["result"] == ValidationResult.PASS)

        return {
            "dimension": "subject_data",
            "label": "受试者数据",
            "checks": checks,
            "passed_count": passed,
            "total_count": len(checks)
        }

    def _check_edc_sync_status(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "edc_sync" in mock_data:
            sync_data = mock_data["edc_sync"]
            success_rate = sync_data.get("success_rate", 100.0)
            total_syncs = sync_data.get("total_syncs", 1000)
            failed_syncs = sync_data.get("failed_syncs", 0)
        else:
            success_rate = 99.8
            total_syncs = 1250
            failed_syncs = 3

        threshold = vc["subject_sync_success_rate_threshold"]
        passed = success_rate >= threshold
        result = ValidationResult.PASS if passed else ValidationResult.FAIL

        detail = {
            "success_rate": success_rate,
            "threshold": threshold,
            "total_syncs": total_syncs,
            "failed_syncs": failed_syncs,
            "period_hours": 24
        }

        suggestion = None
        if not passed:
            suggestion = (f"EDC-CTMS 数据同步成功率低于阈值。建议：1) 检查 EDC 接口可用性；"
                         f"2) 排查失败同步记录并重试；3) 确认网络连接与证书有效性。")

        return {
            "check_item": "edc_ctms_sync_status",
            "label": "EDC-CTMS 数据同步状态",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_subject_field_consistency(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "subject_fields" in mock_data:
            field_data = mock_data["subject_fields"]
            match_rate = field_data.get("match_rate", 100.0)
            total_fields = field_data.get("total_fields", 50)
            mismatch_fields = field_data.get("mismatch_fields", [])
        else:
            match_rate = 100.0
            total_fields = 50
            mismatch_fields = []

        threshold = vc["subject_field_match_threshold"]
        passed = match_rate >= threshold
        result = ValidationResult.PASS if passed else ValidationResult.FAIL

        detail = {
            "match_rate": match_rate,
            "threshold": threshold,
            "total_fields": total_fields,
            "mismatch_fields": mismatch_fields
        }

        suggestion = None
        if not passed:
            suggestion = (f"受试者关键字段一致性低于阈值。建议：1) 逐条核对不匹配字段；"
                         f"2) 确认数据权威来源；3) 执行数据修复后重新校验。")

        return {
            "check_item": "subject_field_consistency",
            "label": "受试者关键字段一致性",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_open_queries(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "open_queries" in mock_data:
            query_data = mock_data["open_queries"]
            open_count = query_data.get("count", 0)
        else:
            open_count = 2

        allowed = vc["open_queries_allowed"]
        passed = open_count <= allowed
        result = ValidationResult.PASS if passed else ValidationResult.WARN

        detail = {
            "open_query_count": open_count,
            "allowed": allowed
        }

        suggestion = None
        if not passed:
            suggestion = f"存在 {open_count} 条未闭环数据 Query。建议：尽快处理并闭环。"

        return {
            "check_item": "open_queries",
            "label": "未闭环数据 Query",
            "result": result,
            "severity": Severity.MEDIUM,
            "blocking": False,
            "detail": detail,
            "suggestion": suggestion
        }

    def validate_ethics_compliance(self, release_id: str, project_id: str,
                                   mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        checks = []
        vc = self.val_config

        checks.append(self._check_ethics_approval_status(mock_data, vc))
        checks.append(self._check_icf_version_validity(mock_data, vc))
        checks.append(self._check_sae_reporting(mock_data, vc))

        passed = sum(1 for c in checks if c["result"] == ValidationResult.PASS)

        return {
            "dimension": "ethics_compliance",
            "label": "伦理合规",
            "checks": checks,
            "passed_count": passed,
            "total_count": len(checks)
        }

    def _check_ethics_approval_status(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "ethics_approvals" in mock_data:
            ethics_data = mock_data["ethics_approvals"]
            total_centers = ethics_data.get("total_centers", 10)
            valid_count = ethics_data.get("valid_count", 10)
            expired_list = ethics_data.get("expired_list", [])
        else:
            total_centers = 10
            valid_count = 10
            expired_list = []

        all_valid = valid_count == total_centers
        result = ValidationResult.PASS if all_valid else ValidationResult.FAIL

        detail = {
            "total_centers": total_centers,
            "valid_count": valid_count,
            "expired_centers": total_centers - valid_count,
            "expired_list": expired_list
        }

        suggestion = None
        if not all_valid:
            suggestion = (f"存在 {total_centers - valid_count} 个中心伦理批件过期。"
                         f"建议：1) 立即更新过期中心的伦理批件；"
                         f"2) 确认是否影响发布范围；"
                         f"3) 提交伦理委员会沟通加急审批。")

        return {
            "check_item": "ethics_approval_status",
            "label": "伦理批件状态",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_icf_version_validity(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "icf_versions" in mock_data:
            icf_data = mock_data["icf_versions"]
            consistent = icf_data.get("consistent", True)
            details = icf_data.get("details", [])
        else:
            consistent = True
            details = []

        result = ValidationResult.PASS if consistent else ValidationResult.FAIL

        detail = {
            "consistent": consistent,
            "details": details
        }

        suggestion = None
        if not consistent:
            suggestion = ("ICF 版本与伦理批准版本不一致。"
                       "建议：1) 核对各中心 ICF 版本；"
                       "2) 确保使用最新伦理批准版本。")

        return {
            "check_item": "icf_version_validity",
            "label": "ICF 版本有效性",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_sae_reporting(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "sae_reports" in mock_data:
            sae_data = mock_data["sae_reports"]
            unreported_count = sae_data.get("unreported_count", 0)
            details = sae_data.get("details", [])
        else:
            unreported_count = 0
            details = []

        allowed = vc["sae_unreported_allowed"]
        passed = unreported_count <= allowed
        result = ValidationResult.PASS if passed else ValidationResult.FAIL

        detail = {
            "unreported_sae_count": unreported_count,
            "allowed": allowed,
            "details": details
        }

        suggestion = None
        if not passed:
            suggestion = (f"存在 {unreported_count} 例 SAE 未及时报告。"
                         f"建议：1) 立即完成 SAE 报告并上报；"
                         f"2) 评估对受试者安全的影响。")

        return {
            "check_item": "sae_reporting",
            "label": "严重不良事件（SAE）报告",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def validate_trial_progress(self, release_id: str, project_id: str,
                                mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        checks = []
        vc = self.val_config

        checks.append(self._check_milestone_consistency(mock_data, vc))
        checks.append(self._check_budget_enrollment_ratio(mock_data, vc))
        checks.append(self._check_center_stall_status(mock_data, vc))

        passed = sum(1 for c in checks if c["result"] == ValidationResult.PASS)

        return {
            "dimension": "trial_progress",
            "label": "试验进度",
            "checks": checks,
            "passed_count": passed,
            "total_count": len(checks)
        }

    def _check_milestone_consistency(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "milestones" in mock_data:
            ms_data = mock_data["milestones"]
            deviation_days = ms_data.get("max_deviation_days", 1)
            details = ms_data.get("details", [])
        else:
            deviation_days = 2
            details = [{"milestone": "入组完成", "baseline": "2024-06-01",
                        "actual": "2024-06-03", "deviation_days": 2}]

        threshold = vc["milestone_deviation_days"]
        passed = abs(deviation_days) <= threshold
        result = ValidationResult.PASS if passed else ValidationResult.WARN

        detail = {
            "max_deviation_days": deviation_days,
            "threshold_days": threshold,
            "details": details
        }

        suggestion = None
        if not passed:
            suggestion = (f"里程碑节点偏差 {deviation_days} 天，超过阈值 {threshold} 天。"
                         f"建议：1) 评估偏差原因；2) 确认是否影响发布计划；"
                         f"3) 与项目组确认是否调整。")

        return {
            "check_item": "milestone_consistency",
            "label": "里程碑节点一致性",
            "result": result,
            "severity": Severity.MEDIUM,
            "blocking": False,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_budget_enrollment_ratio(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "budget_enrollment" in mock_data:
            be_data = mock_data["budget_enrollment"]
            enrollment_rate = be_data.get("enrollment_rate", 45.0)
            budget_rate = be_data.get("budget_rate", 42.0)
            deviation = abs(enrollment_rate - budget_rate)
        else:
            enrollment_rate = 45.0
            budget_rate = 42.0
            deviation = 3.0

        threshold = vc["budget_deviation_percent"]
        passed = deviation <= threshold
        result = ValidationResult.PASS if passed else ValidationResult.WARN

        detail = {
            "enrollment_rate": enrollment_rate,
            "budget_usage_rate": budget_rate,
            "deviation_percent": deviation,
            "threshold_percent": threshold
        }

        suggestion = None
        if not passed:
            suggestion = (f"入组进度与预算耗用偏差 {deviation}%，超过阈值 {threshold}%。"
                         f"建议：1) 分析预算耗用异常原因；2) 评估资源配置合理性。")

        return {
            "check_item": "budget_enrollment_ratio",
            "label": "入组进度 vs 预算耗用",
            "result": result,
            "severity": Severity.MEDIUM,
            "blocking": False,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_center_stall_status(self, mock_data: Dict[str, Any], vc: Dict) -> Dict[str, Any]:
        if mock_data and "center_stall" in mock_data:
            stall_data = mock_data["center_stall"]
            stalled_count = stall_data.get("stalled_count", 0)
            details = stall_data.get("details", [])
        else:
            stalled_count = 0
            details = []

        result = ValidationResult.PASS if stalled_count == 0 else ValidationResult.WARN

        detail = {
            "stalled_centers": stalled_count,
            "stall_threshold_days": vc["center_stall_days"],
            "details": details
        }

        suggestion = None
        if stalled_count > 0:
            suggestion = (f"存在 {stalled_count} 个异常停滞中心。"
                         f"建议：1) 排查停滞原因；2) 推动中心启动进展。")

        return {
            "check_item": "center_stall_status",
            "label": "中心启动进度",
            "result": result,
            "severity": Severity.MEDIUM,
            "blocking": False,
            "detail": detail,
            "suggestion": suggestion
        }

    def validate_electronic_documents(self, release_id: str, project_id: str,
                                   mock_data: Dict[str, Any] = None) -> Dict[str, Any]:
        checks = []

        checks.append(self._check_tmf_document_integrity(mock_data))
        checks.append(self._check_esignature_validity(mock_data))
        checks.append(self._check_audit_trail_integrity(mock_data))

        passed = sum(1 for c in checks if c["result"] == ValidationResult.PASS)

        return {
            "dimension": "electronic_documents",
            "label": "电子文档",
            "checks": checks,
            "passed_count": passed,
            "total_count": len(checks)
        }

    def _check_tmf_document_integrity(self, mock_data: Dict[str, Any]) -> Dict[str, Any]:
        if mock_data and "tmf_documents" in mock_data:
            tmf_data = mock_data["tmf_documents"]
            completeness_rate = tmf_data.get("completeness_rate", 100.0)
            total_core_docs = tmf_data.get("total_core_docs", 80)
            archived_docs = tmf_data.get("archived_docs", 80)
            missing_docs = tmf_data.get("missing_docs", [])
        else:
            completeness_rate = 100.0
            total_core_docs = 80
            archived_docs = 80
            missing_docs = []

        passed = completeness_rate == 100.0
        result = ValidationResult.PASS if passed else ValidationResult.FAIL

        detail = {
            "completeness_rate": completeness_rate,
            "total_core_docs": total_core_docs,
            "archived_docs": archived_docs,
            "missing_docs": missing_docs
        }

        suggestion = None
        if not passed:
            suggestion = (f"TMF 核心文档完整率 {completeness_rate}%。"
                         f"建议：1) 补齐缺失的核心文档；"
                         f"2) 确认文档版本正确性。")

        return {
            "check_item": "tmf_document_integrity",
            "label": "TMF 核心文档完整性",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_esignature_validity(self, mock_data: Dict[str, Any]) -> Dict[str, Any]:
        if mock_data and "esignatures" in mock_data:
            esig_data = mock_data["esignatures"]
            all_valid = esig_data.get("all_valid", True)
            invalid_docs = esig_data.get("invalid_docs", [])
        else:
            all_valid = True
            invalid_docs = []

        result = ValidationResult.PASS if all_valid else ValidationResult.FAIL

        detail = {
            "all_esignatures_valid": all_valid,
            "invalid_documents": invalid_docs
        }

        suggestion = None
        if not all_valid:
            suggestion = (f"存在 {len(invalid_docs)} 份文档电子签名无效。"
                         f"建议：1) 重新签署缺失的电子签名；"
                         f"2) 验证签名证书有效性。")

        return {
            "check_item": "esignature_validity",
            "label": "电子签名有效性",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def _check_audit_trail_integrity(self, mock_data: Dict[str, Any]) -> Dict[str, Any]:
        if mock_data and "audit_trail" in mock_data:
            at_data = mock_data["audit_trail"]
            complete = at_data.get("complete", True)
            issues = at_data.get("issues", [])
        else:
            complete = True
            issues = []

        result = ValidationResult.PASS if complete else ValidationResult.FAIL

        detail = {
            "audit_trail_complete": complete,
            "issues": issues
        }

        suggestion = None
        if not complete:
            suggestion = ("审计追踪存在缺失或篡改痕迹。"
                       "建议：1) 立即核查审计日志完整性；"
                       "2) 确认是否存在异常操作。")

        return {
            "check_item": "audit_trail_integrity",
            "label": "审计追踪完整性",
            "result": result,
            "severity": Severity.HIGH,
            "blocking": True,
            "detail": detail,
            "suggestion": suggestion
        }

    def generate_fix_report(self, validation_result: Dict[str, Any]) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("  发布前置校验 - 修复建议报告")
        lines.append("=" * 60)
        lines.append(f"发布编号: {validation_result.get('release_id', 'N/A')}")
        lines.append(f"校验时间: {validation_result.get('checked_at', 'N/A')}")
        lines.append(f"总体结果: {'阻断发布' if validation_result['summary']['blocked'] else '通过'}")
        lines.append(f"通过: {validation_result['summary']['passed']} / {validation_result['summary']['total']}")
        lines.append(f"失败: {validation_result['summary']['failed']}")
        lines.append(f"警告: {validation_result['summary']['warnings']}")
        lines.append("")

        for dim_key, dim_data in validation_result["dimensions"].items():
            lines.append(f"【{dim_data['label']}】")
            for check in dim_data["checks"]:
                if check["result"] == "PASS":
                    status_icon = "OK"
                elif check["result"] == "FAIL":
                    status_icon = "FAIL"
                else:
                    status_icon = "WARN"
                lines.append(f"  {status_icon} {check['label']} [{check['result']}]")
                if check["result"] != "PASS":
                    lines.append(f"    严重级别: {check['severity']}")
                    lines.append(f"    是否阻断: {'是' if check['blocking'] else '否'}")
                    if check.get('suggestion'):
                        lines.append(f"    修复建议: {check['suggestion']}")
            lines.append("")

        if validation_result["summary"]["blocking_items"]:
            lines.append("【阻断项清单】")
            for i, item in enumerate(validation_result["summary"]["blocking_items"], 1):
                lines.append(f"  {i}. {item['label']}")
            lines.append("")

        return "\n".join(lines)

    def get_validation_result(self, release_id: str) -> Dict[str, Any]:
        result_file = os.path.join(self.validation_dir, f"{release_id}.json")
        if os.path.exists(result_file):
            return read_json_file(result_file)
        return None
