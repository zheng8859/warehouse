"""冷路径开关 路由。

事实来源：19-系统架构与部署视图 附录B；16-数据衔接与 cap 自维护 附录B

阶段五（文档 29）实现：POST /toggle（仅管理员）。默认关闭。

本模块当前只声明 router 对象，**不预置桩端点** —— 端点按 TDD 先写测试再实现
（00-总体开发方案 §3.1），避免用返回 501 的空壳端点冒充已完成。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/llm")
