"""业务路由汇总。

cutover 提供固定路径的切换/工作流接口，必须排在含 /api/{module} 动态路径的
resource 之前注册，否则固定路径会被动态路由抢先匹配。
"""
from __future__ import annotations

from app.routers import cutover, resource

ROUTERS = [cutover, resource]
