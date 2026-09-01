"""Auto Flow RPC sidecar 公共常量。

版本三元组（§16 验收）：appVersion / sidecarVersion / protocolVersion。
前端在启动时比对，不一致应提示（避免协议错配）。
"""
from __future__ import annotations

__version__ = "0.1.0"          # 应用版本（与 Tauri app 对齐，迁移期起名）
PROTOCOL_VERSION = 1          # JSON-RPC 协议版本，破坏性变更时 +1
