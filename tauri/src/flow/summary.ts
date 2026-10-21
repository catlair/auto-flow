// 画布节点卡片上那行「一眼看懂这个节点在干什么」的摘要。
//
// 为什么需要它：流程图上一堆卡片只显示「延迟 / 鼠标 / 条件」，用户得逐个点开
// 看参数才知道哪条路径是干什么的。摘要把参数里最能说明问题的几个值摊在卡片上。
//
// 刻意做成**与节点类型无关**的通用逻辑：按 ParamDef 顺序取前几个有值的参数，
// 不做每种节点的专用格式化。理由和节点注册那条一样——专用格式化表会随着
// 新增节点悄悄过时，而通用的永远不会。代价是表达力弱一点，可以接受。

import type { NodeDefinition, WorkflowNode } from "@/rpc/types";
// 相对路径而不是 `@/flow/duration`：Node 测试用 transpileModule + 裸 require
// 直接跑这个模块，拿不到 vite 的 `@` 别名（`@/rpc/types` 是 type-only，
// 编译后整条被删掉，所以别名在那里没事，值导入不行）。
import { formatDuration } from "./duration";
import { recDisplayName } from "./recording";

/** 摘要最多显示几个参数值。 */
export const SUMMARY_MAX_PARTS = 3;
/** 摘要的字符上限（卡片宽度有限，超出即截断）。 */
export const SUMMARY_MAX_CHARS = 48;

function formatValue(v: unknown, ptype?: string): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string" && v.trim() === "") return "";
  // 时长参数的原始值是毫秒总数（5400000），卡片上直接显示数字没人看得懂。
  // 这是**按控件类型**格式化，不是按节点类型——本模块「与节点类型无关」的性质没破。
  if (ptype === "duration") return formatDuration(v);
  if (typeof v === "string") return v.trim();
  if (typeof v === "boolean") return v ? "开" : "关";
  if (typeof v === "number") return String(v);
  // 事件数组：对外视图是 {count}，本地真源是数组，两种都要能显示。
  // **空数组返回空串**：引用录制库的节点把 `events` 存成 `[]`（事件在那条录制里），
  // 显示「0 个事件」会让卡片看起来像「这个节点没有内容」，而其实它引用着一段录制。
  if (Array.isArray(v)) return v.length ? `${v.length} 个事件` : "";
  if (typeof v === "object") {
    const c = (v as { count?: unknown }).count;
    if (typeof c === "number") return c > 0 ? `${c} 个事件` : "";
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
    // 引用录制库的节点，参数值是**一个 id**（`20260915-190433-a1b2c3`）——卡片上
    // 显示它没人认得出是哪段操作。后端已在 `recording_name` 里捎了名字，优先用它。
    // 仍然只按 `ptype` 分支，没有引入「按节点类型」的格式化表。
    const text =
      p.ptype === "recording"
        ? recDisplayName(node.params)
        : formatValue(node.params?.[p.key], p.ptype);
    if (text) parts.push(text);
  }
  const joined = parts.join(" · ");
  return joined.length > SUMMARY_MAX_CHARS
    ? joined.slice(0, SUMMARY_MAX_CHARS - 1) + "…"
    : joined;
}
