/**
 * 录制库在界面上的纯逻辑：下拉选项怎么拼、当前值不在库里时怎么办。
 *
 * 单独成模块的理由同 `sidebar.ts`：这里全是「界面不报错、但用户会读错」的东西——
 * 两条同名录制在下拉里分不出来、录制被删掉后下拉变成空白（看着像「没选」，
 * 实际节点上还挂着那个 id）、时长写成 `90000ms`。抽出来就能直接跑。
 */

import { formatDuration } from "./duration";

/** 录制库里一条录制的元信息（**不含 events**，见后端 recording_lib.list_meta）。 */
export interface RecMeta {
  id: string;
  name: string;
  created_at?: string;
  count: number;
  duration_ms: number;
  origin: [number, number];
}

/**
 * `record_replay` 的 `source` 参数取值。
 *
 * ⚠️ 这是**跨语言协议**：后端 `tasks/builtin.py` 的 `ParamDef("source", ..., ["内嵌", "录制库"])`
 * 与 `RecordReplayTask.run()` 里的字面量必须一模一样。对不上不会报错，
 * 只会让「选了录制库」静默退回内嵌（于是回放一个空序列）。
 */
export const INLINE_SOURCE = "内嵌";
export const LIB_SOURCE = "录制库";

/** 该节点是否按 id 引用库里的录制。缺 `source`（老流程文件）按「内嵌」算。 */
export function isLibSource(params: Record<string, any> | null | undefined): boolean {
  return String(params?.source ?? INLINE_SOURCE) === LIB_SOURCE;
}

/** 时长：`90000` → "1分30秒"；与参数面板共用 duration.ts 的那份实现，不再抄第二遍。 */
export function fmtRecDuration(ms: unknown): string {
  return formatDuration(ms);
}

/**
 * 下拉里的一项：**名字 + 条数**。
 *
 * 只显示名字的话，两条都叫「录制 2026-09-15 19:04:33」的条目（同一秒里录了两段、
 * 或复制过库目录）在选择器里完全一样，选哪个全靠猜。
 */
export function recOptionLabel(m: Partial<RecMeta> | null | undefined): string {
  const name = String(m?.name ?? "").trim() || "(未命名)";
  const n = Number(m?.count ?? 0);
  return `${name}（${Number.isFinite(n) ? n : 0} 条）`;
}

export interface RecOption {
  value: string;
  label: string;
  /** 该值在库里已经不存在了（被删），只为把当前选中项显示出来而兜底补的一项。 */
  missing?: boolean;
}

/**
 * 下拉选项：库里的条目 + **当前值的兜底项**。
 *
 * 兜底项不能省。录制被删掉之后，节点的 `recording_id` 还在，而 t-select 在
 * 「值不在选项里」时显示成**空白**——看起来就是「这个字段没填」。用户于是去
 * 重选一条，或者压根没发现这里引着一个已经不存在的录制（回放时才报错）。
 * 补一项并标灰，至少让他看见「这里本来选的是 X，但它没了」。
 */
export function recOptions(
  items: RecMeta[] | null | undefined,
  currentId: unknown
): RecOption[] {
  const list = Array.isArray(items) ? items : [];
  const out: RecOption[] = list.map((m) => ({ value: String(m.id), label: recOptionLabel(m) }));
  const cur = String(currentId ?? "").trim();
  if (cur && !out.some((o) => o.value === cur)) {
    out.push({ value: cur, label: `${cur}（已不在录制库）`, missing: true });
  }
  return out;
}

/**
 * 画布卡片上显示的录制名。
 *
 * 节点里只存 id，后端 `_public_node` 会顺手把名字捎在 `recording_name` 上
 * （走缓存，不会每次广播都扫库）。拿不到就退化成空串——卡片上显示一串
 * `20260915-190433-a1b2c3` 等于没写，不如不写。
 */
export function recDisplayName(params: Record<string, any> | null | undefined): string {
  return String(params?.recording_name ?? "").trim();
}
