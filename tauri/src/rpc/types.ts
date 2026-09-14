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
  /**
   * 后端生成的稳定唯一 id。**必须用 uid 做 key，不能用 type 或 index**：
   * 边的两端就是 uid，用 index 会在增删后把连线接到别的节点上。
   */
  uid: string;
  /** 自定义名（空则 UI 显示类型名） */
  name?: string;
  /** 画布坐标（后端只存不算，布局由前端负责） */
  x?: number;
  y?: number;
}

/**
 * 流程图的一条有向边（v4）。
 *
 * `src` / `dst` 都是节点 **uid**。同一个 `(src, port)` 只允许一条边——
 * 后端 `edge.add` 会替换旧的，前端在替换时给出提示。
 */
export interface WorkflowEdge {
  src: string;
  /** 源节点的出口名，见 `@/flow/ports`（out / true / false / case:N / else） */
  port: string;
  /** 目标节点 uid；空串 = 该出口悬空（与「这条边不存在」在执行上等价） */
  dst: string;
}

export interface Workflow {
  name: string;
  speed: number;
  repeat: number;
  nodes: WorkflowNode[];
  /** 有向边；v4 起「下一步走哪儿」完全由它决定 */
  edges: WorkflowEdge[];
  /** 起始节点 uid；空 = 第一个 start 节点，再退回 nodes[0] */
  start: string;
  /**
   * 本次加载是否由旧版「有序列表」迁移而来。**不落盘**，只在内存里标记，
   * 用于提示用户「条件不再自动门控，请重连」。
   */
  migrated_from_list?: boolean;
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
  tooltip?: string;
  /**
   * 条件显示：仅当 `params[key]` 满足条件时才渲染该字段。
   * 分支节点用它按 `case_count` 收起多余的 case 字段（6 个 case × 2 个字段
   * 平铺出来是一堵墙）。
   */
  show_if?: { key: string; gte?: number; lte?: number; eq?: any } | null;
  /**
   * 在可编辑文本框旁挂一个「选择文件」按钮。
   * 用于「既可能是模板图路径、也可能是一段文字」的字段——`file` 类型是只读的，
   * 装不下文字，所以只能给 text 加选择按钮。
   */
  pick?: boolean;
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
