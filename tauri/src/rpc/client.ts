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
} from "./types";

class RpcClient {
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (v: any) => void; reject: (e: any) => void }
  >();
  private buffer = "";
  private listeners = new Set<RpcNotificationHandler>();
  private unlisten: UnlistenFn | null = null;
  private connected = false;

  async connect(): Promise<void> {
    if (this.unlisten) return;
    this.unlisten = await listen<string>("rpc_event", (e) =>
      this.onChunk(e.payload)
    );
    this.connected = true;
  }

  onNotify(h: RpcNotificationHandler): () => void {
    this.listeners.add(h);
    return () => this.listeners.delete(h);
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
      invoke("send_rpc", { line: JSON.stringify(req) }).catch((e) => {
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

export const rpc = new RpcClient();
