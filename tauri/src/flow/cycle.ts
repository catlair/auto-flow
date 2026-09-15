// 环检测（纯函数，无 Vue/DOM 依赖）。
//
// 背景：v4 **允许回边**——循环是一等公民（「等到某个条件成立再往下走」这类流程
// 只能靠回边表达），兜底是 `Executor.MAX_STEPS = 10000`。所以这里**只提示、不拦**，
// 任何「不让连成环」的做法都会把合法用法一起挡掉。
//
// 那为什么还值得提示：**「故意做循环」和「连错成环」在画布上长得一模一样**。
// 区别要到运行时才显现，而且是「跑很久之后撞到步数上限被强制停下」——
// 到那时候用户已经不知道是哪一步连错了。所以最有价值的时机是**刚连上的那一刻**。
//
// ⚠️ 判据是「**图上有环**」，不是「一定会执行的死循环」。后者不可判定：
// 分支节点没被选中的 `case:N` 上的环永远不会走到。所以文案必须是
// 「出现了环，确认一下」，不能写成「这是死循环」。

import type { GraphEdge } from "./ports";

/**
 * 从 `from` 出发沿边能不能走到 `to`（`from === to` 视为能到，即零长路径）。
 *
 * `dst` 为空串表示「这个出口留空」（后端允许这么存，与「边不存在」在执行上等价），
 * 它不指向任何节点，直接跳过——否则会凭空连出一个幽灵节点。
 *
 * 用**广度优先 + 迭代队列**而不是递归：图可以很大（也允许有环），
 * 递归会在长链上爆栈，而有环时更会直接无限递归。
 */
export function reaches(edges: readonly GraphEdge[], from: string, to: string): boolean {
  if (from === to) return true;
  // 先建邻接表：一次遍历，而不是每访问一个节点就扫一遍边表。
  const out = new Map<string, string[]>();
  for (const e of edges) {
    if (!e.dst) continue;
    const list = out.get(e.src);
    if (list) list.push(e.dst);
    else out.set(e.src, [e.dst]);
  }
  const seen = new Set([from]);
  const queue: string[] = [from];
  for (let i = 0; i < queue.length; i++) {
    for (const nxt of out.get(queue[i]) ?? []) {
      if (nxt === to) return true;
      if (seen.has(nxt)) continue;
      seen.add(nxt);
      queue.push(nxt);
    }
  }
  return false;
}

/**
 * 在 `edges` 之上再连一条 `src → dst`，会不会形成环。
 *
 * 判据只有一句：**`dst` 已经能走到 `src`**（含 `dst === src` 的自环）。
 * 不必把整张图查一遍环——连线前的图是无环的（任何环都是某次连线造出来的），
 * 所以**新出现的环必然包含这条新边**，而「含新边的环」⟺「存在 `dst ⇝ src` 的路径」。
 *
 * `edges` 传**连线前**的边即可：同一个 `(src, port)` 的旧边会被后端替换掉，
 * 但它从 `src` 出发，而这里问的是「能不能走到 `src`」——路径一碰到 `src` 就结束，
 * 根本用不到那条旧边。（自环由 `isValidConnection` 与后端一起拦掉，这里仍判它，
 * 是为了让这个函数单独看也是对的。）
 *
 * ⚠️ 调用方应当传**活边**（`liveEdges` 的结果）：被 `case_count` 调小后残留的
 * `case:4` 边画布上不显示、执行器也不会走，拿它判环会提示一个用户根本看不见的环。
 */
export function createsCycle(edges: readonly GraphEdge[], src: string, dst: string): boolean {
  if (!dst) return false; // 悬空出口不构成边
  return reaches(edges, dst, src);
}
