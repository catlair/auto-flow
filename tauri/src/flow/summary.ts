// 画布节点卡片上那行「一眼看懂这个节点在干什么」的摘要。
//
// 为什么需要它：流程图上一堆卡片只显示「延迟 / 鼠标 / 条件」，用户得逐个点开
// 看参数才知道哪条路径是干什么的。摘要把参数里最能说明问题的几个值摊在卡片上。
//
// 刻意做成**与节点类型无关**的通用逻辑：按 ParamDef 顺序取前几个有值的参数，
// 不做每种节点的专用格式化。理由和节点注册那条一样——专用格式化表会随着
// 新增节点悄悄过时，而通用的永远不会。代价是表达力弱一点，可以接受。

import type { NodeDefinition, WorkflowNode } from "@/rpc/types";

/** 摘要最多显示几个参数值。 */
export const SUMMARY_MAX_PARTS = 3;
/** 摘要的字符上限（卡片宽度有限，超出即截断）。 */
export const SUMMARY_MAX_CHARS = 48;

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v.trim();
  if (typeof v === "boolean") return v ? "开" : "关";
  if (typeof v === "number") return String(v);
  // 事件数组：对外视图是 {count}，本地真源是数组，两种都要能显示
  if (Array.isArray(v)) return `${v.length} 个事件`;
  if (typeof v === "object") {
    const c = (v as { count?: unknown }).count;
    if (typeof c === "number") return `${c} 个事件`;
    return "";
  }
  return String(v);
}

/**
 * 生成节点摘要。没有可用参数时返回空串（调用方据此不渲染那一行）。
 *
 * `defs` 传 `store.nodeDefsByName`：定义没拉到（后端刚崩/正在重连）时
 * 拿不到 ParamDef，此时退化为「不显示摘要」而不是抛错——画布必须能在
 * 后端不可用时照常渲染，否则连「看一眼工作流长什么样」都做不到。
 */
export function nodeSummary(
  node: WorkflowNode | null | undefined,
  defs: Record<string, NodeDefinition> | null | undefined
): string {
  if (!node) return "";
  const def = defs?.[node.type];
  if (!def || !Array.isArray(def.params)) return "";

  const parts: string[] = [];
  for (const p of def.params) {
    if (parts.length >= SUMMARY_MAX_PARTS) break;
    const text = formatValue(node.params?.[p.key]);
    if (text) parts.push(text);
  }
  const joined = parts.join(" · ");
  return joined.length > SUMMARY_MAX_CHARS
    ? joined.slice(0, SUMMARY_MAX_CHARS - 1) + "…"
    : joined;
}
