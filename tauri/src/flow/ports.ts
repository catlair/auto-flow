// 流程图出口名（port）——与后端 `core/events.py` 的常量一一对应。
//
// 出口名是**协议的一部分**：前端按它渲染连接手柄、按它提交边，后端按它选下一跳。
// 两边的字面量必须一致，所以这里不写死字符串，一律引用本模块的常量。
//
// 为什么要有这个模块：手柄渲染（几个出口、叫什么）与连线校验（这个出口合不合法）
// 是两处逻辑，散在两个组件里迟早会不一致。这里集中定义，两处都调它。

/** 操作 / 开始节点的唯一出口。 */
export const PORT_OUT = "out";
/** 条件节点：成立。 */
export const PORT_TRUE = "true";
/** 条件节点：不成立。 */
export const PORT_FALSE = "false";
/** 分支节点：所有 case 都不成立。 */
export const PORT_ELSE = "else";

// ---------------------------------------------------------------------------
// 目标落点侧（边从目标节点的哪一侧进入）
//
// 与后端 `core/events.py` 的 TARGET_SIDE_* 一一对应，靠 flow-ports.test.mjs 比对。
// **纯画布展示语义，不参与执行**：执行器只认 (src, port) → dst。
//
// 为什么要持久化：画布多边都能落点，用户在哪儿松手线就从哪侧画进去。不存的话
// 落库后只能画到固定一侧——**拖拽预览**用你松手的那侧、**落库后**却画到另一侧，
// 用户会看到连线「跳」一下。
//
// 为什么**没有右侧**：右边是输出侧（出口手柄都在卡片右边缘）。单出口节点的出口
// 手柄正好在右边缘中线，与右目标手柄**同点重合**——Vue Flow 按「离指针最近的手柄」
// 判定落点，重合时行为不稳定，表现是「有时连得上有时连不上」。故入口只开放左/上/下。
// ---------------------------------------------------------------------------

export const TARGET_SIDE_LEFT = "left";
export const TARGET_SIDE_TOP = "top";
export const TARGET_SIDE_BOTTOM = "bottom";
/** 顺序即画布上的展示顺序。 */
export const TARGET_SIDES: string[] = [
  TARGET_SIDE_LEFT,
  TARGET_SIDE_TOP,
  TARGET_SIDE_BOTTOM,
];
/** 默认侧（也是旧文件没这个字段时的取值）：左侧，与历史行为一致。 */
export const DEFAULT_TARGET_SIDE = TARGET_SIDE_LEFT;

/**
 * 把任意输入收敛成合法的落点侧；空值/未知值回落到默认侧。
 *
 * 必须**收敛**而不是原样用：脏值会让 `targetHandle` 指向一个不存在的手柄，
 * Vue Flow 找不到锚点就画不出这条边（只在控制台刷告警）——用户看到的是
 * 「连线莫名消失」，比画错一侧严重得多。
 */
export function normalizeTargetSide(v: unknown): string {
  const s = String(v ?? "").trim();
  return TARGET_SIDES.includes(s) ? s : DEFAULT_TARGET_SIDE;
}

/** 分支节点第 i 个 case 的出口名（i 从 1 开始，与用户看到的序号一致）。 */
export function casePort(i: number): string {
  return `case:${i}`;
}

/** 分支节点的 case 上限。与后端 `core.events.MAX_BRANCH_CASES` 必须一致。 */
export const MAX_BRANCH_CASES = 6;
/** 分支节点的 case 下限（只有 1 个 case 的分支等价于条件节点）。 */
export const MIN_BRANCH_CASES = 2;

const CASE_RE = /^case:(\d+)$/;

/** 一个出口在画布上的显示名。 */
export function portLabel(port: string): string {
  if (port === PORT_TRUE) return "成立";
  if (port === PORT_FALSE) return "不成立";
  if (port === PORT_ELSE) return "其他";
  const m = CASE_RE.exec(port);
  if (m) return `情形 ${m[1]}`;
  return "下一步";
}

/** 把任意输入收敛成合法的 case 数（后端也做同样的钳制）。 */
export function clampCaseCount(v: unknown): number {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return MIN_BRANCH_CASES;
  return Math.min(Math.max(n, MIN_BRANCH_CASES), MAX_BRANCH_CASES);
}

/**
 * 某类节点的出口名列表，**按画布上从上到下的顺序**。
 *
 * `caseCount` 只对 branch 有意义（它是节点参数，用户可改）。
 * 未知类型按操作节点处理（唯一出口 `out`）——比返回空数组安全：
 * 返回空数组会让节点一个手柄都没有，直接连不出线。
 *
 * `condition_group` 与 `condition` 共用 `true`/`false`：两者对用户都是
 * 「一个判断、两条路」，出口形状一致才不用在画布上区分两种连线方式。
 * 出口名相同也意味着**后端的边表不用为新节点做任何特殊处理**。
 */
export function exitPorts(type: string, caseCount: unknown = MIN_BRANCH_CASES): string[] {
  if (type === "condition" || type === "condition_group") return [PORT_TRUE, PORT_FALSE];
  if (type === "branch") {
    const n = clampCaseCount(caseCount);
    const out: string[] = [];
    for (let i = 1; i <= n; i++) out.push(casePort(i));
    out.push(PORT_ELSE);
    return out;
  }
  // 结束节点没有出口：它是路径的终点，渲染出手柄只会让人误以为还能往下走。
  if (type === "end") return [];
  return [PORT_OUT];
}

/** 该出口名对这类节点是否合法（用于校验外部传入的边）。 */
export function isValidPort(type: string, port: string, caseCount?: unknown): boolean {
  return exitPorts(type, caseCount).includes(port);
}

/** 该节点能否作为边的起点（结束节点不能）。 */
export function canConnectFrom(type: string): boolean {
  return exitPorts(type).length > 0;
}

/**
 * 该节点能否作为边的终点。
 *
 * 开始节点不能：它是流程入口，有入边意味着「还能从别处跳进来」，
 * 而执行器的入口是 `start` 字段，入边根本不会被走到——画布上看着连通、
 * 实际不会执行，是最容易骗人的那种不一致。
 */
export function canConnectTo(type: string): boolean {
  return type !== "start";
}

// ---------------------------------------------------------------------------
// 活边：画布会渲染、执行器也会走的那些边
// ---------------------------------------------------------------------------

/** 判断「哪些边算数」所需的最小节点形状（结构兼容 `WorkflowNode`）。 */
export interface GraphNode {
  uid: string;
  type: string;
  params?: Record<string, unknown>;
}

/** 判断「哪些边算数」所需的最小边形状（结构兼容 `WorkflowEdge`）。 */
export interface GraphEdge {
  src: string;
  port: string;
  dst: string;
  /** 画布落点侧。`liveEdges` **原样透传**边对象，所以这个字段跟着一起出来。 */
  dst_side?: string;
}

/**
 * 从一堆边里挑出**画布会渲染、执行器也会走**的那些（下称「活边」）。
 *
 * 排除两类：
 * - **两端的节点不存在**（只有手工编辑过的 json 才可能出现）——画出来是悬空的；
 * - **出口名对源节点已不合法**。`case_count` 从 5 调回 3 之后，`case:4`/`case:5`
 *   上的边**仍留在文件里**（这是刻意的：调回去它还在）。但那个手柄不存在，
 *   Vue Flow 找不到锚点会在控制台刷告警、并把线画在奇怪的位置；执行器也不会走到
 *   它（`next_map` 按 `(src, port)` 查表，而分支根本不会发出那个出口）。
 *
 * 为什么要抽成一个函数：画布渲染（`FlowCanvas.vue` 的 `vEdges`）和环检测都要用
 * 同一套「哪些边算数」的判断。各写一份迟早会漂，而漂了的表现是
 * 「画布上明明看着没环、却提示有环」——这种没人能想明白的现象。
 */
export function liveEdges(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[]
): GraphEdge[] {
  const byUid = new Map(nodes.map((n) => [n.uid, n]));
  return edges.filter((e) => {
    const src = byUid.get(e.src);
    if (!src || !byUid.has(e.dst)) return false;
    return exitPorts(src.type, src.params?.case_count).includes(e.port);
  });
}
