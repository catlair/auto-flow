// 置顶日志窗的**共享纯逻辑**（无 Vue / 无 Tauri / 无 DOM 依赖，node 测试可直接求值）。
//
// 为什么单独一个模块：主窗（生产者）和日志窗（消费者）必须认同同一套
// 「哪些通知算日志、每行怎么排版、历史留多长」。两边各写一份的话，日志窗显示的
// 会与主窗的运行轨迹对不上——而这份日志的全部价值就是「和界面上看到的是同一件事」。
//
// 通道用 Tauri 事件：两个窗口是**各自独立的 WebView**，没有共享内存。事件也**不缓存、
// 不重放**，所以晚开的日志窗看不到开窗前的行，需要主窗按 `LOG_HELLO_EVENT` 补一份历史。

export type LogLevel = "info" | "ok" | "warn" | "error";

export type LogLine = {
  /** 本地时间戳（ms），只用于显示 */
  t: number;
  level: LogLevel;
  /**
   * 来源标记：后端通知的 method（`run.node` / `run.finished`…）、
   * `progress`（只进表头进度条，不占列表行）、`front`（前端自己产生的事件）。
   */
  kind: string;
  text: string;
};

/** 主窗 → 日志窗：一行。 */
export const LOG_EVENT = "af_log";
/** 主窗 → 日志窗：开窗时一次性补历史（`LogLine[]`）。 */
export const LOG_BACKLOG_EVENT = "af_log_backlog";
/** 日志窗 → 主窗：「我刚挂载，把历史给我」。 */
export const LOG_HELLO_EVENT = "af_log_hello";

/**
 * 日志窗保留的行数上限。
 *
 * 一次长跑几千步，全留着会一直涨内存，而且滚不动（要翻回半小时前只能靠搜索）。
 * 400 行 ≈ 一屏 20 行 × 20 屏，够回看「刚才那步为什么失败」。
 */
export const LOG_CAP = 400;

/** 单行文本上限：异常里带整段 JSON 的情况见过，不截会把一行撑成一屏。 */
export const LOG_TEXT_MAX = 300;

/**
 * 压平空白并截断。
 *
 * 必须压空白：后端 message 里带换行（如 stderr 尾巴、traceback），
 * 而列表行是单行的——不压就会把行高撑开、后续行全被挤下去。
 */
export function clip(text: string, max = LOG_TEXT_MAX): string {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : s.slice(0, max) + "…";
}

/** 后端日志级别 → 界面颜色档。认不出的级别一律按 info（宁可少着色，不要乱报红）。 */
export function levelOf(level: string): LogLevel {
  const u = String(level ?? "").toUpperCase();
  if (u === "ERROR" || u === "CRITICAL" || u === "FATAL") return "error";
  if (u === "WARNING" || u === "WARN") return "warn";
  if (u === "OK") return "ok";
  return "info";
}

/** `HH:MM:SS`。只到秒——毫秒在滚动列表里是噪声，而行首的时间是用来对顺序的。 */
export function fmtClock(t: number): string {
  const d = new Date(t);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
}

function mkLine(t: number, level: LogLevel, kind: string, text: string): LogLine | null {
  const s = clip(text);
  return s ? { t, level, kind, text: s } : null;
}

/**
 * 一条后端通知 → 一行日志；不该进日志窗的返回 `null`。
 *
 * `t` 由调用方传入（默认 now）：测试要可控，且**同一批**通知共用同一时刻更接近
 * 真实——它们本来就是同一瞬间到达的。
 */
export function notificationToLine(
  n: { method?: string; params?: any },
  t = Date.now()
): LogLine | null {
  const method = String(n?.method ?? "");
  const p = n?.params ?? {};
  switch (method) {
    case "run.node": {
      const name = String(p.name || p.type || "?").trim();
      const detail = String(p.detail || "").trim();
      return mkLine(t, "info", method, detail ? name + " · " + detail : name);
    }
    case "run.progress": {
      const total = Number(p.total ?? 0);
      if (!(total > 0)) return null;
      // 进度不进列表：它每一步都来一条，会把真正的节点日志冲掉。
      // 交给表头显示「3/10」即可。
      return { t, level: "info", kind: "progress", text: Number(p.done ?? 0) + "/" + total };
    }
    case "run.finished":
      return mkLine(t, p.stopped ? "warn" : "ok", method, p.stopped ? "运行已停止" : "运行结束");
    case "run.error":
      return mkLine(
        t,
        "error",
        method,
        "运行出错：" + String(p.message ?? "") + (p.type ? "（节点 " + p.type + "）" : "")
      );
    case "log.warning":
      return mkLine(
        t,
        levelOf(String(p.level ?? "WARNING")),
        method,
        (p.logger ? String(p.logger) + "：" : "") + String(p.message ?? "")
      );
    case "schedule.fired":
      return p.ran
        ? mkLine(t, "ok", method, "定时触发：" + String(p.path ?? ""))
        : mkLine(t, "warn", method, "定时触发跳过：" + String(p.reason ?? "忙"));
    case "record.stopped":
      return mkLine(t, "ok", method, "录制结束：" + Number(p.count ?? 0) + " 条");
    default:
      return null;
  }
}

/**
 * 有界环形缓冲：主窗留一份历史，供日志窗「开窗时补历史」。
 *
 * 为什么是环形而不是数组 + shift：日志是**高频**写入（运行中每步 1～2 条），
 * 超限时 `shift()` 每次搬整个数组，长跑下会变成可见的卡顿。
 */
export class LogRing {
  readonly cap: number;
  private buf: LogLine[] = [];
  constructor(cap = LOG_CAP) {
    this.cap = Math.max(1, Math.floor(cap));
  }
  push(line: LogLine): void {
    this.buf.push(line);
    if (this.buf.length > this.cap) this.buf.splice(0, this.buf.length - this.cap);
  }
  /** 副本：调用方拿去 emit，不能拿到内部数组（否则后续 push 会改到它）。 */
  snapshot(): LogLine[] {
    return this.buf.slice();
  }
  clear(): void {
    this.buf = [];
  }
  get size(): number {
    return this.buf.length;
  }
}
