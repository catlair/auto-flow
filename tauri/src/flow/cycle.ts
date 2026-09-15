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
// 光说「有环」还不够：节点一多，用户拿着两个节点名照样找不到那条回路。
// 所以这里返回的是**环上的边本身**（`cycleEdges`），画布把它们标出来——
// 提示要能落到具体的东西上，否则等于把「找环」这件事原样推回给用户。
//
// ⚠️ 判据是「**图上有环**」，不是「一定会执行的死循环」。后者不可判定：
// 分支节点没被选中的 `case:N` 上的环永远不会走到。所以文案必须是
// 「出现了环，确认一下」，不能写成「这是死循环」。

import type { GraphEdge } from "./ports";

/**
 * 从 `from` 走到 `to` 的**一条**路径（返回途经的每条边，按行走顺序）；
 * 走不到返回 `null`。`from === to` 视为零长路径，返回空数组。
 *
 * 这是本模块唯一的遍历原语——布尔问题（「能不能到」）与找环都从它派生。
 * 单独再写一个「只回 true/false」的版本等于把同一段 BFS 抄两遍，
 * 而两份遍历迟早会漂（漂了的表现是「提示说没环、高亮却说有环」）。
 *
 * `dst` 为空串表示「这个出口留空」（后端允许这么存，与「边不存在」在执行上等价），
 * 它不指向任何节点，直接跳过——否则会凭空连出一个幽灵节点。
 *
 * 用**广度优先 + 迭代队列**而不是递归：图可以很大（用例跑到 20000 节点），
 * 递归会在长链上爆栈，而有环时更会直接无限递归。
 *
 * 用 BFS 而不是 DFS 还有一个产品上的理由：**多条回路时给的是最短那条**，
 * 也就是画布上最紧凑、最好认的那个圈。DFS 可能给出一条绕半个图的路径。
 */
export function pathTo(
  edges: readonly GraphEdge[],
  from: string,
  to: string
): GraphEdge[] | null {
  if (from === to) return [];
  // 先建邻接表（存**边**而不是只存目标节点：回溯时要靠它还原路径）：
  // 一次遍历，而不是每访问一个节点就扫一遍边表。
  const out = new Map<string, GraphEdge[]>();
  for (const e of edges) {
    if (!e.dst) continue;
    const list = out.get(e.src);
    if (list) list.push(e);
    else out.set(e.src, [e]);
  }
  // 节点 → 「是从哪条边走到它的」。回溯时顺着它一路退回来就是路径。
  const parent = new Map<string, GraphEdge>();
  const seen = new Set([from]);
  const queue: string[] = [from];
  for (let i = 0; i < queue.length; i++) {
    for (const e of out.get(queue[i]) ?? []) {
      if (seen.has(e.dst)) continue;
      seen.add(e.dst);
      parent.set(e.dst, e);
      // 一发现 `to` 就回溯：BFS 保证这是最短的一条，再往下找只会更长。
      // 注意顺序——`to` 一旦被发现就立刻返回，所以它永远不会被压进队列，
      // 也就不会出现「`to` 已被 seen 过」而漏掉返回的情况。
      if (e.dst === to) {
        const path: GraphEdge[] = [];
        let cur = to;
        while (cur !== from) {
          const via = parent.get(cur);
          if (!via) return null; // 不可能发生：parent 与 seen 同步维护
          path.push(via);
          cur = via.src;
        }
        return path.reverse();
      }
      queue.push(e.dst);
    }
  }
  return null;
}

/**
 * 在 `edges` 之上再连一条 `next`，会不会形成环；会则返回**这条环上的全部边**，
 * 不会返回 `null`。
 *
 * 返回的数组是**一条闭合走法**：从 `next.src` 出发，沿 `next` 到 `next.dst`，
 * 再沿 `edges` 里的路径走回 `next.src`。所以它满足
 * 「`edges[i].dst === edges[i+1].src`（相邻首尾相接）」且「最后一条的 `dst` 回到起点」。
 * `next` 一定在数组第 0 位——调用方据此知道哪条是刚连的那条。
 *
 * 判据只有一句：**`next.dst` 已经能走回 `next.src`**（含 `dst === src` 的自环）。
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
export function cycleEdges(
  edges: readonly GraphEdge[],
  next: GraphEdge
): GraphEdge[] | null {
  if (!next.dst) return null; // 悬空出口不构成边
  const back = pathTo(edges, next.dst, next.src);
  if (!back) return null;
  return [next, ...back];
}
