// 与后端 rpc/server.py + controller.py 对齐的 TS 类型（§9 / §3.1 / §3.2）。

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id?: number;
  method: string;
  params?: any;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: any;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number;
  result?: any;
  error?: JsonRpcError;
}

/** 通知帧：有 method 且无 id（或 id 为 null）。 */
export interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params: any;
}

export type RpcFrame = JsonRpcResponse | JsonRpcNotification;

export interface Permissions {
  accessibility: boolean;
  inputMonitoring: boolean;
  screenRecording: boolean;
}

export interface WorkflowNode {
  type: string;
  params: Record<string, any>;
  enabled: boolean;
  /** 后端生成的稳定唯一 id：列表渲染/拖拽 key 不得用 type 或 index */
  uid?: string;
  /** 自定义名（空则 UI 显示类型名） */
  name?: string;
}

export interface Workflow {
  name: string;
  speed: number;
  repeat: number;
  nodes: WorkflowNode[];
  variables?: Record<string, any>;
}

export type ParamType =
  | "int"
  | "float"
  | "bool"
  | "select"
  | "text"
  | "file"
  | "keys"
  | "events";

export interface ParamDef {
  key: string;
  label: string;
  ptype: ParamType;
  default?: any;
  options?: string[];
  [k: string]: any;
}

export interface NodeDefinition {
  type: string;
  name: string;
  /** 菜单排序键（§9.4）：后端已按此升序返回，前端照单渲染即可，不要再排。 */
  order: number;
  params: ParamDef[];
  common_params: ParamDef[];
}

export type RpcNotificationHandler = (n: JsonRpcNotification) => void;

/**
 * sidecar 上下线回调（Rust 侧以 Tauri 事件 rpc_up / rpc_down 广播，
 * 与 stdout 上的 NDJSON 通知分属两条通道）。detail 为断连原因，
 * 可能含查找路径与 sidecar 最近 stderr。
 */
export type RpcStatusHandler = (up: boolean, detail: string) => void;

export const RPC_ERROR_MESSAGES: Record<number, string> = {
  [-32000]: "内部错误",
  [-32601]: "未知方法",
  [-32602]: "参数无效",
  [-32700]: "协议解析错误",
  [-32001]: "已在运行",
  [-32002]: "录制中无法运行",
  [-32003]: "未处于录制状态",
  [-32004]: "工作流为空",
  [-32005]: "缺少权限",
};
