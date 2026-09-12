/**
 * 定高虚拟列表的窗口计算（纯函数，无 Vue/DOM 依赖，便于单测）。
 *
 * 录制事件流可达 `RECORD_BUFFER_LIMIT` 条，直接 `v-for` 会让 DOM 节点数随事件数
 * 增长——录制中每秒可能新增上百条，长录制后页面会明显卡顿。这里只算出「当前
 * 视口需要渲染哪一段」，配合一个撑高的占位层即可。
 *
 * 行高固定，所以窗口边界是纯算术，不需要测量每个元素。
 */

export interface WindowRange {
  /** 起始下标（含） */
  start: number;
  /** 结束下标（不含） */
  end: number;
  /** 顶部占位高度（px）：`start` 之前的行占的空间 */
  offsetY: number;
  /** 全部内容的总高度（px） */
  totalH: number;
}

export interface WindowOptions {
  /** 总条数 */
  count: number;
  /** 单行高度（px），必须 > 0 才能虚拟化 */
  itemH: number;
  /** 视口高度（px）。挂载前可能还是 0 */
  viewportH: number;
  /** 当前滚动位置（px） */
  scrollTop: number;
  /** 视口上下各多渲染几行，避免快速滚动时露白 */
  overscan?: number;
}

export const DEFAULT_OVERSCAN = 8;

/**
 * 算出需要渲染的 `[start, end)` 区间与占位高度。
 *
 * 边界情形都收敛到「不会渲染空列表、也不会越界」：
 * - `count <= 0` → 空窗口；
 * - `itemH` 非法（NaN/0/负数）→ 退化为全量渲染（宁可慢也不白屏）；
 * - `viewportH` 还没量到（0）→ 同样退化为全量渲染，等 ResizeObserver 报尺寸；
 * - `scrollTop` 超出范围 → 夹到 `[0, totalH - viewportH]`。
 */
export function computeWindow(opts: WindowOptions): WindowRange {
  const { count, itemH, viewportH, scrollTop } = opts;
  const overscan = Math.max(0, opts.overscan ?? DEFAULT_OVERSCAN);

  if (!Number.isFinite(count) || count <= 0) {
    return { start: 0, end: 0, offsetY: 0, totalH: 0 };
  }
  // 行高非法时无法定位，退化为全量渲染（不虚拟化），避免算出一段错位的窗口。
  if (!Number.isFinite(itemH) || itemH <= 0) {
    return { start: 0, end: count, offsetY: 0, totalH: 0 };
  }

  const totalH = count * itemH;
  const vh = Number.isFinite(viewportH) && viewportH > 0 ? viewportH : 0;
  if (vh === 0) {
    return { start: 0, end: count, offsetY: 0, totalH };
  }

  const maxTop = Math.max(0, totalH - vh);
  const rawTop = Number.isFinite(scrollTop) ? scrollTop : 0;
  const top = Math.min(Math.max(0, rawTop), maxTop);

  const first = Math.floor(top / itemH);
  // +1 是因为窗口顶端/底端通常各露出一行的一部分。
  const visible = Math.ceil(vh / itemH) + 1;
  const start = Math.max(0, first - overscan);
  const end = Math.min(count, first + visible + overscan);

  return { start, end: Math.max(end, Math.min(count, start + 1)), offsetY: start * itemH, totalH };
}
