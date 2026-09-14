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
 */
export function exitPorts(type: string, caseCount: unknown = MIN_BRANCH_CASES): string[] {
  if (type === "condition") return [PORT_TRUE, PORT_FALSE];
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
