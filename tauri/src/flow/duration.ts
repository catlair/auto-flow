// 时长（毫秒总数 ⇄ 时/分/秒/毫秒）与时刻（HH:mm:ss）的纯逻辑。
//
// 为什么单独成模块：这两块只在参数面板（ParamsPanel.vue）里用，而那是 SFC，
// Node 内置的测试跑不了它——`node --test` 不做 .vue 编译。把**会算错的那部分**
// 挪到无 Vue 依赖的模块里，就能像 sidebar.ts 一样直接测（见 tests/duration.test.mjs）。
//
// 关键不变式：时长对外的**存储格式始终是一个毫秒整数**（delay 节点的 `ms`）。
// 四格输入框只是渲染层，读写都经由这里换算——这样已保存的流程文件零迁移，
// 后端 run() 也不用改。

/** 时长的四格单位，顺序即渲染顺序：时 → 分 → 秒 → 毫秒。 */
export const DUR_UNITS = [
  // div：一个该单位等于多少毫秒；mod：超过多少要向上进位（0 = 不进位，即无上限）
  { key: "h", label: "时", div: 3600000, mod: 0, max: 9999 },
  { key: "m", label: "分", div: 60000, mod: 60, max: 59 },
  { key: "s", label: "秒", div: 1000, mod: 60, max: 59 },
  { key: "ms", label: "毫秒", div: 1, mod: 1000, max: 999 },
] as const;

export type DurUnit = (typeof DUR_UNITS)[number];

/** 时刻选择器统一用 HH:mm:ss。后端 `_seconds_until` 两种都收（秒可省）。 */
export const TIME_FORMAT = "HH:mm:ss";
export const TIME_DEFAULT = "09:00:00";

/**
 * 洗成非负整数。空值/非数字/负数一律 0——输入框可以被清空，`Number("")` 是 0
 * 而不是 NaN，这里统一走一条路，免得下游各写一遍判断还写得不一致。
 */
function toMs(v: unknown): number {
  const n = Math.floor(Number(v));
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/**
 * 取某一格的数值：`60000` 在「分」格是 1，在「秒」格是 0（60 秒进一位）。
 *
 * 「时」不取模：等 3 小时是合理需求，不该被折算成 3 时 / 0 分。
 */
export function partOf(v: unknown, unit: DurUnit): number {
  const n = Math.floor(toMs(v) / unit.div);
  return unit.mod ? n % unit.mod : n;
}

/**
 * 只替换 `unit` 那一段，其余单位原样保留。
 *
 * **不能**「读四格 → 各自相加 → 写回」：用户改「分」时，另外三格的输入框可能
 * 还没提交（数字框失焦才提交），相加会把它们当成 0，把时/秒/毫秒一起清掉。
 * 只动一段就没有这个窗口期。
 */
export function withPart(v: unknown, unit: DurUnit, next: unknown): number {
  const total = toMs(v);
  const cur = partOf(total, unit);
  const want = Math.max(0, Math.floor(Number(next) || 0));
  return Math.max(0, total - cur * unit.div + want * unit.div);
}

/**
 * 画布卡片摘要用：`500` → "500毫秒"、`5400000` → "1时30分"、`0` → "0毫秒"。
 *
 * 截断到前 `maxParts` 个非零单位。摘要那行只有约 48 字，`1时30分0秒0毫秒` 里
 * 后两个 0 纯属噪音——参数面板上看得见，卡片上不必重复。
 */
export function formatDuration(v: unknown, maxParts = 2): string {
  const total = toMs(v);
  const hit = DUR_UNITS.map((u) => ({ label: u.label, n: partOf(total, u) })).filter(
    (u) => u.n > 0
  );
  if (hit.length === 0) return "0毫秒";
  return hit
    .slice(0, maxParts)
    .map((u) => `${u.n}${u.label}`)
    .join("");
}

/**
 * 时刻洗成 `HH:mm:ss`，给选择器用。
 *
 * 老文件里存的是 `HH:mm`（改控件前是手输的），TDesign 的 TimePicker 会拿
 * `format` 去校验 `value`，`"09:00"` 对 `HH:mm:ss` 是**非法值**、会被悄悄重置——
 * 用户打开旧流程会发现时刻字段空了。所以进选择器前先补齐秒。
 */
export function normalizeTime(v: unknown): string {
  const s = String(v ?? "").trim();
  const m = /^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/.exec(s);
  if (!m) return TIME_DEFAULT;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  const sec = m[3] === undefined ? 0 : Number(m[3]);
  if (h > 23 || mi > 59 || sec > 59) return TIME_DEFAULT;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(h)}:${p(mi)}:${p(sec)}`;
}
