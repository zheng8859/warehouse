"""FastAPI 依赖注入。

事实来源：13-权限分级与访问控制系统 §六
  get_db                  → 数据库会话
  current_account         → 解析会话凭据得到当前账号（第 1 层认证的产物）
  require(resource, action) → 第 2 层资源级权限检查（路线图 RBAC 的挂载点）

本文件为骨架占位。
"""
