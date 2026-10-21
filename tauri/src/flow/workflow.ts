/**
 * 工作流库在界面上的纯逻辑：列表副标题怎么拼、导出时的默认文件名怎么取、
 * 后端给了一份坏数据时怎么不白屏。
 *
 * 单独成模块的理由同 `recording.ts`：这里全是「界面不报错、但用户会读错」的东西——
 * 名字里带 `/` 时导出的默认路径会把文件悄悄写进一个子目录、`updated_at` 格式一变
 * 列表上就显示一片空白的时间、后端少给一个字段整个弹层白屏。抽出来就能直接跑。
 */

/** 工作流库里一份工作流的元信息（**不含 nodes/edges**，见后端 workflow_lib.list_meta）。 */
export interface WorkflowMeta {
  id: string;
  name: string;
  created_at?: string;
  updated_at?: string;
  node_count: number;
  edge_count: number;
}

export const UNTITLED = "未命名工作流";

/** 标题栏 / 列表里显示的名字；空名字一律显示占位，不显示空白。 */
export function wfDisplayName(name: unknown): string {
  return String(name ?? "").trim() || UNTITLED;
}

/**
 * `2026-09-15 11:20:33` → `09-15 11:20`。
 *
 * 用**字符串切片**而不是 `new Date(...)`：`"2026-09-15 11:20:33"`（空格分隔）
 * 不是 ECMAScript 规定的日期格式，V8 收、别的引擎可能收成 Invalid Date——
 * 那时列表上会显示 "Invalid Date"，而这在 macOS 上永远复现不出来。
 */
export function fmtStamp(s: unknown): string {
  const t = String(s ?? "").trim();
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(t);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : "";
}

/** 列表里一行的副标题：节点数 / 连线数 / 最近修改。 */
export function wfSubtitle(m: Partial<WorkflowMeta> | null | undefined): string {
  const n = Math.max(0, Number(m?.node_count ?? 0) || 0);
  const e = Math.max(0, Number(m?.edge_count ?? 0) || 0);
  const parts = [n ? `${n} 个节点` : "空流程", `${e} 条连线`];
  const when = fmtStamp(m?.updated_at);
  if (when) parts.push(`修改于 ${when}`);
  return parts.join(" · ");
}

/**
 * 导出的默认文件名。
 *
 * `/` 与 `:` 在 macOS 上分别是路径分隔符与 Finder 的保留字符：默认文件名里带着
 * 它们时，保存对话框会把文件**悄悄写进一个子目录**（或直接拒绝），而用户以为
 * 自己只是「沿用了原来的名字」。开头的点还要去掉——那会变成一个隐藏文件，
 * 导完在 Finder 里根本看不见。
 */
export function wfExportName(name: unknown): string {
  const base = String(name ?? "")
    .replace(/[/:\\]/g, "_")
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f]/g, "")
    .replace(/^[.\s]+/, "")
    .trim()
    .slice(0, 80)
    .trim();
  return (base || UNTITLED) + ".json";
}

/**
 * 把后端给的列表规整成能安全渲染的形状。
 *
 * 后端一条坏文件会被跳过，但「列表里少一条」和「整个弹层白屏」的代价差得很远：
 * 一份手工改坏的文件让 `node_count` 变成字符串、`id` 变成数字，渲染时
 * `m.id` 拼进 key 就足以让 Vue 抛错。
 */
export function normalizeMetas(raw: unknown): WorkflowMeta[] {
  if (!Array.isArray(raw)) return [];
  const out: WorkflowMeta[] = [];
  for (const m of raw) {
    if (!m || typeof m !== "object") continue;
    const id = String((m as any).id ?? "").trim();
    if (!id) continue;
    out.push({
      id,
      name: wfDisplayName((m as any).name),
      created_at: String((m as any).created_at ?? ""),
      updated_at: String((m as any).updated_at ?? ""),
      node_count: Math.max(0, Number((m as any).node_count ?? 0) || 0),
      edge_count: Math.max(0, Number((m as any).edge_count ?? 0) || 0),
    });
  }
  return out;
}

/**
 * 标题栏上的工作流名字，带未保存标记。
 *
 * 标记必须**显式**出现在名字里，不能只靠一个圆点：用户看的是「这行字」，
 * 一个 6 像素的点在浅色标题栏上很容易被当成渲染瑕疵。
 */
export function wfTitle(name: unknown, dirty: boolean): string {
  return wfDisplayName(name) + (dirty ? " ●" : "");
}
