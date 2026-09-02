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
///
/// `stderr_tail` 保留 sidecar 最后若干行 stderr。早前 stderr 被 `Stdio::null()` 直接丢弃，
/// sidecar 崩溃（Python 异常 / dylib 加载失败 / 路径不存在）时界面上只剩一句笼统提示，
/// 排障只能靠猜——这正是「请求授权失败：undefined」难以定位的原因。
struct SidecarState {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<std::process::ChildStdin>>,
    stderr_tail: Mutex<Vec<String>>,
    /// 末次上下线状态 (up, detail)。Tauri 事件不缓存、不重放：sidecar 若在前端注册
    /// 监听前就已启动失败，那个 rpc_down 会永久丢失，故留一份状态供前端主动查询。
    last_status: Mutex<(bool, String)>,
}

/// 把外壳侧的关键事件（sidecar 候选路径、spawn 结果、stderr、断连）落到文件。
///
/// 为什么必须落盘：从 Finder 启动的 App，其 stdout/stderr 都不可见；headless 开发环境
/// 又看不到 GUI——没有这个文件，sidecar 起不来时（如「请求授权失败」那次）就只能靠猜。
/// 位置：`$HOME/Library/Logs/autoflow-tauri.log`，追加写。
fn log_line(msg: &str) {
    use std::io::Write as _;
    let Some(home) = std::env::var_os("HOME") else {
        return;
    };
    let dir = std::path::PathBuf::from(home).join("Library").join("Logs");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("autoflow-tauri.log"))
    else {
        return;
    };
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let _ = writeln!(f, "[{ts}] {msg}");
}

/// 拼出 sidecar 路径（见 docs §12 固定路径），并留开发态回退：
/// `tauri dev` 时 resource_dir 指向 target/debug/，那里没有打包资源，
/// 回退到源码树内由 build_sidecar.sh 放置的 `src-tauri/autoflow-sidecar/`。
fn sidecar_exe(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("autoflow-sidecar").join("autoflow-sidecar"));
    }
    candidates.push(
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("autoflow-sidecar")
            .join("autoflow-sidecar"),
    );
    log_line(&format!(
        "sidecar 候选路径: {:?}",
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
    ));
    let found = candidates.into_iter().find(|p| p.exists());
    log_line(&format!(
        "sidecar 选中: {}",
        found
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "(均不存在)".into())
    ));
    found
}

/// 记录 + 广播上下线状态。
fn set_status(app: &tauri::AppHandle, state: &Arc<SidecarState>, up: bool, detail: String) {
    *state.last_status.lock().unwrap() = (up, detail.clone());
    let _ = if up {
        app.emit("rpc_up", detail)
    } else {
        app.emit("rpc_down", detail)
    };
}

/// 真正 spawn + 接管 stdout/stdin；stdout 逐行 emit `rpc_event`，EOF 后守护重启。
fn spawn_and_watch(app: &tauri::AppHandle, state: &Arc<SidecarState>) {
    let Some(exe) = sidecar_exe(app) else {
        set_status(app, state, false, "resource_dir 解析失败".into());
        return;
    };
    let mut child = match Command::new(&exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => {
            log_line(&format!("sidecar 已启动 pid={}", c.id()));
            c
        }
        Err(e) => {
            // 带上实际查找路径：sidecar 曾落在 Resources/resources/autoflow-sidecar（双 resources
            // 错位），此时只给一句笼统提示会让排查成本极高。
            log_line(&format!(
                "sidecar 启动失败：{e}（查找路径 {}）",
                exe.display()
            ));
            set_status(
                app,
                state,
                false,
                format!("sidecar 启动失败：{e}（查找路径 {}）", exe.display()),
            );
            return;
        }
    };

    // stderr 接管：实时 emit 供诊断面板，同时留一份尾部缓冲，崩溃时随 rpc_down 上报。
    if let Some(stderr) = child.stderr.take() {
        let h = app.clone();
        let st = state.clone();
        thread::spawn(move || {
            let reader = std::io::BufReader::new(stderr);
            for line in reader.lines().flatten() {
                {
                    let mut tail = st.stderr_tail.lock().unwrap();
                    tail.push(line.clone());
                    if tail.len() > 50 {
                        tail.remove(0);
                    }
                }
                log_line(&format!("sidecar_stderr: {line}"));
                let _ = h.emit("sidecar_stderr", line);
            }
        });
    }

    let stdout = child.stdout.take().unwrap();
    *state.stdin.lock().unwrap() = child.stdin.take();
    *state.child.lock().unwrap() = Some(child);
    set_status(app, state, true, String::new());

    let h = app.clone();
    let st = state.clone();
    thread::spawn(move || {
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines().flatten() {
            // 每条 stdout 行即一帧（后端 compact NDJSON，见 §13）；前端负责半包切分兜底。
            let _ = h.emit("rpc_event", line);
        }
        // stdout 关闭 = sidecar 退出。必须做两件事（此前都漏了）：
        //   ① 发 rpc_down——否则前端永远收不到断连提示，只会看到权限全 false 的横幅；
        //   ② 清掉失效的 stdin 句柄——否则 send_rpc 报含糊的 broken pipe，而非"未连接"。
        log_line("sidecar stdout EOF（进程退出）");
        *st.stdin.lock().unwrap() = None;
        *st.child.lock().unwrap() = None;
        let tail = st.stderr_tail.lock().unwrap().clone();
        let detail = if tail.is_empty() {
            String::new()
        } else {
            format!("\n最近 stderr：\n{}", tail.join("\n"))
        };
        set_status(&h, &st, false, format!("sidecar 已退出，正在重连…{detail}"));
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
        Some(stdin) => stdin
            .write_all(format!("{line}\n").as_bytes())
            .and_then(|_| stdin.flush())
            .map_err(|e| {
                log_line(&format!("send_rpc 写入失败: {e}（请求: {line}）"));
                e.to_string()
            }),
        None => {
            log_line(&format!("send_rpc 失败: sidecar 未连接（请求: {line}）"));
            Err("sidecar 未连接".into())
        }
    }
}

/// 查询末次上下线状态，返回 `[up, detail]`。
/// 用途：Tauri 事件不缓存也不重放，sidecar 若在前端注册监听前就启动失败，
/// 那次 rpc_down 会永久丢失；前端连上后主动查一次即可补齐。
#[tauri::command]
fn rpc_status(app: tauri::AppHandle) -> (bool, String) {
    let state = app.state::<Arc<SidecarState>>();
    // 先绑局部变量再返回：直接把 `.lock().unwrap().clone()` 作为尾表达式，
    // 临时 MutexGuard 会存活到块结束、晚于 `state` 被 drop，触发 E0597。
    let snapshot = state.last_status.lock().unwrap().clone();
    snapshot
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            log_line("=== Auto Flow 外壳启动 ===");
            let handle = app.handle().clone();
            let state = Arc::new(SidecarState {
                child: Mutex::new(None),
                stdin: Mutex::new(None),
                stderr_tail: Mutex::new(Vec::new()),
                last_status: Mutex::new((false, String::new())),
            });
            app.manage(state.clone());
            spawn_and_watch(&handle, &state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_rpc, rpc_status])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
