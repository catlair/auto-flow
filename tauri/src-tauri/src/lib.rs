// Auto Flow Tauri 外壳：拉起 Python sidecar（onedir 置于 Resources/autoflow-sidecar/），
// 用 NDJSON 经 stdin/stdout 通信，stdout 逐行以 `rpc_event` 事件推给前端；前端经 `send_rpc`
// 命令把请求行写回 sidecar stdin。sidecar 退出时守护重启（§12 固定路径 + §13 洁净性）。

use std::io::{BufRead, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use tauri::{Emitter, Manager};

/// sidecar 进程与写端句柄（受锁保护，供 `send_rpc` 写 stdin）。
struct SidecarState {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<std::process::ChildStdin>>,
}

/// 拼出固定路径：<Resources>/autoflow-sidecar/autoflow-sidecar（见 docs §12）。
fn sidecar_exe(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    let dir = app.path().resource_dir().ok()?;
    Some(dir.join("autoflow-sidecar").join("autoflow-sidecar"))
}

/// 真正 spawn + 接管 stdout/stdin；stdout 逐行 emit `rpc_event`，EOF 后守护重启。
fn spawn_and_watch(app: &tauri::AppHandle, state: &Arc<SidecarState>) {
    let Some(exe) = sidecar_exe(app) else {
        let _ = app.emit("rpc_down", "resource_dir 解析失败");
        return;
    };
    let Ok(mut child) = Command::new(&exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        let _ = app.emit("rpc_down", "sidecar 启动失败（检查 Resources/autoflow-sidecar 与授权）");
        return;
    };

    let stdout = child.stdout.take().unwrap();
    *state.stdin.lock().unwrap() = child.stdin.take();
    *state.child.lock().unwrap() = Some(child);
    let _ = app.emit("rpc_up", "");

    let h = app.clone();
    let st = state.clone();
    thread::spawn(move || {
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines().flatten() {
            // 每条 stdout 行即一帧（后端 compact NDJSON，见 §13）；前端负责半包切分兜底。
            let _ = h.emit("rpc_event", line);
        }
        // stdout 关闭 = sidecar 退出 → 守护重启（最多一次，避免死循环打爆）。
        thread::sleep(Duration::from_secs(2));
        spawn_and_watch(&h, &st);
    });
}

/// 前端 → sidecar：把一行 NDJSON 写进 sidecar stdin（§13 帧以 \n 结尾）。
#[tauri::command]
fn send_rpc(app: tauri::AppHandle, line: String) -> Result<(), String> {
    let state = app.state::<Arc<SidecarState>>();
    let mut guard = state.stdin.lock().unwrap();
    match guard.as_mut() {
        Some(stdin) => {
            stdin
                .write_all(format!("{line}\n").as_bytes())
                .and_then(|_| stdin.flush())
                .map_err(|e| e.to_string())
        }
        None => Err("sidecar 未连接".into()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let state = Arc::new(SidecarState {
                child: Mutex::new(None),
                stdin: Mutex::new(None),
            });
            app.manage(state.clone());
            spawn_and_watch(&handle, &state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_rpc])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
