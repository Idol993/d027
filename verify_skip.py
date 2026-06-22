import sys
import os
sys.path.insert(0, '.')
from common.utils import load_config
from modules.audit import AuditLogger
from modules.approval import ApprovalWorkflow

config = load_config()
audit = AuditLogger(config)
aw = ApprovalWorkflow(config, audit)

# 创建审批流
flow = aw.create_approval_flow(
    release_id="TEST-SKIP-001",
    version="2.3.0",
    project_id="CT-2024-001",
    applicant="test_user"
)

print("=== 初始状态 ===")
for node in flow["nodes"]:
    print(f"  {node['order']}. {node['label']}: {node['status']}")

print("\n=== 测试：直接审批 PM（跳过临床、数据、质控） ===")
try:
    flow2 = aw.approve("TEST-SKIP-001", "pm", "pm_chen", "直接跳过审批")
    print("  ❌ 严重错误：PM审批竟然通过了！")
    print(f"  PM节点状态: {flow2['nodes'][3]['status']}")
except PermissionError as e:
    print(f"  ✓ 正确被拒绝")
    print(f"  错误信息: {e}")
except Exception as e:
    print(f"  ⚠  其他错误: {type(e).__name__}: {e}")

print("\n=== 验证数据没有被修改 ===")
flow3 = aw._load_approval_flow("TEST-SKIP-001")
for node in flow3["nodes"]:
    print(f"  {node['order']}. {node['label']}: {node['status']}")
