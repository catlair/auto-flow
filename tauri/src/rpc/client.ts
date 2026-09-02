// NDJSON JSON-RPC 2.0 客户端（§13 stdio 洁净性 + 半包/粘包切分）。
//
// - 帧经 Tauri `rpc_event` 事件（Rust 侧把 sidecar stdout 逐行 emit）投递；但 Rust 不一定
//   按行边界切片，故前端仍累积缓冲、按 \n 切分，残留半行留待下一块（与后端对称）。
// - request() 返回 Promise，按自增 id 匹配响应；通知（无 id）走 onNotify 订阅。
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import type {
  JsonRpcNotification,
  JsonRpcRequest,
  JsonRpcResponse,
  RpcNotificationHandler,
  RpcStatusHandler,
} from "./types";

class RpcClient {
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (v: any) => void; reject: (e: any) => void }
  >();
  private buffer = "";
  private listeners = new Set<RpcNotificationHandler>();
  private statusListeners = new Set<RpcStatusHandler>();
  private stderrListeners = new Set<(line: string) => void>();
  private unlisten: UnlistenFn | null = null;
  private unlistenLifecycle: UnlistenFn[] = [];
  private connected = false;

  async connect(): Promise<void> {
    if (this.unlisten) return;
    this.unlisten = await listen<string>("rpc_event", (e) =>
      this.onChunk(e.payload)
    );
    // 进程生命周期由 Rust 侧以 Tauri 事件广播（rpc_up / rpc_down / sidecar_stderr），
    // 与 stdout 上的 NDJSON 通知是两条不同通道。早前只监听了 rpc_event，导致 sidecar
    // 起不来或崩溃时前端毫无感知——只能看到"权限全 false"的横幅，排障无从下手。
    this.unlistenLifecycle.push(
      await listen("rpc_up", () => this.setStatus(true, ""))
    );
    this.unlistenLifecycle.push(
      await listen<string>("rpc_down", (e) =>
        this.setStatus(false, e.payload ?? "")
      )
    );
    this.unlistenLifecycle.push(
      await listen<string>("sidecar_stderr", (e) =>
        this.stderrListeners.forEach((h) => h(e.payload))
      )
    );
    // 补齐竞态：Rust 的 rpc_down 可能在上面三步 listen() 注册完成**之前**就已发出
    // （sidecar 启动即失败正是此场景），而 Tauri 事件不缓存、不重放，那次事件会永久丢失。
    // 故主动查一次末次状态。
    let up = true;
    try {
      const [u, detail] = (await invoke("rpc_status")) as [boolean, string];
      up = !!u;
      if (!up) this.statusListeners.forEach((h) => h(false, detail ?? ""));
    } catch {
      /* 外壳不含该命令（旧版）时忽略，退化为仅靠事件 */
    }
    this.connected = up;
  }

  private setStatus(up: boolean, detail: string): void {
    this.connected = up;
    this.statusListeners.forEach((h) => h(up, detail));
  }

  onNotify(h: RpcNotificationHandler): () => void {
    this.listeners.add(h);
    return () => this.listeners.delete(h);
  }

  /** 订阅 sidecar 上下线（Rust 侧 rpc_up / rpc_down）。 */
  onStatus(h: RpcStatusHandler): () => void {
    this.statusListeners.add(h);
    return () => this.statusListeners.delete(h);
  }

  /** 订阅 sidecar stderr 原文（诊断用）。 */
  onStderr(h: (line: string) => void): () => void {
    this.stderrListeners.add(h);
    return () => this.stderrListeners.delete(h);
  }

  get isConnected(): boolean {
    return this.connected;
  }

  /** §13：累积缓冲，按 \n 切分，最后一段若不完整则留待下一块。 */
  private onChunk(chunk: string): void {
    this.buffer += chunk;
    let idx: number;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      const t = line.trim();
      if (t) this.dispatch(t);
    }
  }

  private dispatch(line: string): void {
    let frame: any;
    try {
      frame = JSON.parse(line);
    } catch {
      console.error("[rpc] 非法 JSON 帧:", line);
      return;
    }
    // 通知：有 method 且无有效 id
    if ("method" in frame && (frame.id === undefined || frame.id === null)) {
      this.listeners.forEach((h) => h(frame as JsonRpcNotification));
      return;
    }
    const resp = frame as JsonRpcResponse;
    if (typeof resp.id !== "number") return;
    const p = this.pending.get(resp.id);
    if (!p) return;
    this.pending.delete(resp.id);
    if (resp.error) {
      const err = new Error(resp.error.message);
      (err as any).code = resp.error.code;
      (err as any).data = resp.error.data;
      p.reject(err);
    } else {
      p.resolve(resp.result);
    }
  }

  request(method: string, params?: any): Promise<any> {
    const id = this.nextId++;
    const req: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      invoke("send_rpc", { line: JSON.stringify(req) }).catch((e: unknown) => {
        this.pending.delete(id);
        reject(e);
      });
    });
  }

  /** 单向请求（如 app.shutdown），不关心响应。 */
  async notify(method: string, params?: any): Promise<void> {
    const req: JsonRpcRequest = { jsonrpc: "2.0", method, params };
    await invoke("send_rpc", { line: JSON.stringify(req) });
  }
}

/**
 * 统一提取错误文案。
 *
 * 踩过的坑：Tauri `invoke` 失败时 reject 的值**不一定是 Error**——Rust 命令返回
 * `Err(String)` 时，前端拿到的是裸字符串（如 "sidecar 未连接"）。直接写
 * `(e as Error).message` 会得到 `undefined`，界面上就是「请求授权失败：undefined」。
 * 故统一走这里：string 原样用，Error 取 message，对象取 message，兜底 JSON/字符串化。
 */
export function errMessage(e: unknown): string {
  if (e == null) return "未知错误";
  if (typeof e === "string") return e || "未知错误";
  if (e instanceof Error) return e.message || String(e);
  if (typeof e === "object") {
    const anyE = e as Record<string, unknown>;
    if (typeof anyE.message === "string" && anyE.message) return anyE.message;
    try {
      return JSON.stringify(e);
    } catch {
      return String(e);
    }
  }
  return String(e);
}

export const rpc = new RpcClient();
