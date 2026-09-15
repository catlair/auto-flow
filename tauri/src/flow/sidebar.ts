/**
 * 右侧侧栏的状态：标签页、宽度、开合。
 *
 * 单独成模块而不是留在 App.vue 里，是因为这里的坑全是「界面不报错、但重开就变样」
 * ——宽度越界、存进去的东西读不出来、读到一个已经不存在的标签页。放在组件里
 * 只能靠「手动拖一下、关掉再开一次」验证；抽出来就能直接跑。
 */

export type TabKey = "params" | "run" | "record";

/** 顺序即侧栏上的顺序。 */
export const TABS: { key: TabKey; label: string }[] = [
  { key: "params", label: "参数" },
  { key: "run", label: "运行" },
  { key: "record", label: "录制" },
];

export const LS_KEY = "af.side";
export const MIN_W = 280;
export const MAX_W = 620;
export const DEFAULT_W = 340;

export interface SideState {
  w: number;
  open: boolean;
  tab: TabKey;
}

export const DEFAULT_SIDE: SideState = { w: DEFAULT_W, open: true, tab: "params" };

export function isTab(v: unknown): v is TabKey {
  return TABS.some((t) => t.key === v);
}

/** 宽度钳制。非数字（没设过 / 存坏了 / NaN）回默认值，而不是原样穿下去。 */
export function clampWidth(v: unknown): number {
  // 空值要单独挡掉：Number("") / Number(null) 都是 0，会被钳到**下限**上去，
  // 于是「没设过」和「拖到最窄」变成同一个结果。
  if (v === null || v === undefined || (typeof v === "string" && v.trim() === "")) {
    return DEFAULT_W;
  }
  const n = Number(v);
  if (!Number.isFinite(n)) return DEFAULT_W;
  return Math.min(MAX_W, Math.max(MIN_W, n));
}

/**
 * 读侧栏状态。解析失败或形状不对一律回默认值——这是**启动路径**上的代码，
 * 宁可用户回到默认布局，也不能让整个界面起不来。
 *
 * 注意 `open` 用的是 `!== false`：只有明确存了 `false` 才算收起，
 * 字段缺失（老版本、手工改过）要按展开处理。
 */
export function parseSide(raw: string | null): SideState {
  try {
    const obj = JSON.parse(raw ?? "") as unknown;
    if (typeof obj !== "object" || obj === null) throw new Error("not an object");
    const o = obj as Record<string, unknown>;
    return {
      w: clampWidth(o.w),
      open: o.open !== false,
      // 存过的标签页可能已经不存在了（改过枚举），落回「参数」
      tab: isTab(o.tab) ? o.tab : "params",
    };
  } catch {
    return { ...DEFAULT_SIDE };
  }
}

/** 写侧栏状态。写不进去（隐私模式等）由调用方吞掉。宽度在这里也钳一次。 */
export function serializeSide(s: SideState): string {
  return JSON.stringify({ w: clampWidth(s.w), open: s.open, tab: s.tab });
}

/** 拖侧栏左边缘：宽度作用在右栏上，所以**向左拖 = 变宽**（取负）。 */
export function widthFromDrag(startW: number, startX: number, nowX: number): number {
  return clampWidth(startW - (nowX - startX));
}

/**
 * 选中节点后该停在哪一页。
 *
 * 侧栏收起时**不强切**：展开侧栏是用户自己的动作，不该顺手把页签也跳到「参数」，
 * 否则「我展开侧栏」和「我换了页」两件事会混在一起。
 */
export function tabAfterSelect(cur: TabKey, sideOpen: boolean): TabKey {
  return sideOpen ? "params" : cur;
}
