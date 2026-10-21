// Auto Flow Tauri 外壳：拉起 Python sidecar（onedir 置于 Resources/autoflow-sidecar/），
// 用 NDJSON 经 stdin/stdout 通信，stdout 逐行以 `rpc_event` 事件推给前端；前端经 `send_rpc`
// 命令把请求行写回 sidecar stdin。sidecar 退出时守护重启（§12 固定路径 + §13 洁净性）。

use std::io::{BufRead, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use tauri::menu::{Menu, MenuItem};
use tauri::{Emitter, Manager, WindowEvent};

/// 托盘图标的 id。只在两处用到，但两处都必须一致，所以提成常量。
const TRAY_ID: &str = "af-tray";
/// 托盘菜单项 id。前端**不认识**它们（只认 `tray_action` 事件），
/// 但 Rust 侧要用它区分点了哪一项。
const MENU_SHOW: &str = "af_show";
const MENU_TOGGLE_RECORD: &str = "af_toggle_record";
const MENU_QUIT: &str = "af_quit";

/// 托盘上需要**动态改文案**的句柄。
///
/// 为什么存句柄而不是每次 `tray.menu().get(id)` 取回来：`TrayIcon` 只有 `set_menu`、
/// 没有菜单 getter，想拿回菜单项就得自己再存一份 `Menu`，反而多一层持有点。
/// `MenuItem` 是 `Arc` 包装的，存一份不影响 Tauri 自己那份。
struct TrayHandles {
    toggle: MenuItem<tauri::Wry>,
}

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

/// 日志用的帧预览：长帧（如 nodes.definitions 的 8KB JSON）只留前 max 个字符，
/// 避免每次启动都往日志灌大段 JSON。按字符截取，避免 byte 切片劈开 UTF-8 panic。
fn preview(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        let head: String = s.chars().take(max).collect();
        format!("{head}…(共{}字节)", s.len())
    }
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
            // 每条 stdout 行即一帧（后端 compact NDJSON，见 §13）。
            // 【关键】lines() 会剥掉换行符，而前端 onChunk 按 \n 切分缓冲——若 emit 的
            // payload 不带 \n，帧会永远滞留在前端缓冲区，dispatch 一次都不执行，
            // 表现为「所有请求挂起、权限横幅全 ✗」。必须把 \n 补回去。
            log_line(&format!("rpc_event: {}", preview(&line, 400)));
            let _ = h.emit("rpc_event", format!("{line}\n"));
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
    log_line(&format!("send_rpc 请求: {}", preview(&line, 160)));
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

/// 主窗边界（逻辑点 x,y,w,h）。前端在录制开始/窗口移动时下发给后端做
/// 窗口内事件过滤。为什么不走前端 window API：outerPosition/scaleFactor
/// 需要 core:window capability，缺权限时静默失败 → 后端收不到边界，
/// 「点停止按钮被录进事件」的过滤完全失效。Rust 侧无权限问题。
#[tauri::command]
fn window_bounds(app: tauri::AppHandle) -> Result<(f64, f64, f64, f64), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    let pos = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    Ok((
        pos.x as f64 / scale,
        pos.y as f64 / scale,
        size.width as f64 / scale,
        size.height as f64 / scale,
    ))
}

/// 回放防误触：运行期间主窗开启鼠标穿透（点击落到窗下方的目标上），
/// 运行结束由前端关闭。录制时保持可点（用户要点停止按钮）。
#[tauri::command]
fn set_click_through(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    window
        .set_ignore_cursor_events(enabled)
        .map_err(|e| e.to_string())
}

/// 查询末次上下线状态，返回 `[up, detail]`。
/// 用途：Tauri 事件不缓存也不重放，sidecar 若在前端注册监听前就启动失败，
/// 那次 rpc_down 会永久丢失；前端连上后主动查一次即可补齐。
#[tauri::command]
fn rpc_status(app: tauri::AppHandle) -> (bool, String) {
    log_line("rpc_status 查询");
    let state = app.state::<Arc<SidecarState>>();
    // 先绑局部变量再返回：直接把 `.lock().unwrap().clone()` 作为尾表达式，
    // 临时 MutexGuard 会存活到块结束、晚于 `state` 被 drop，触发 E0597。
    let snapshot = state.last_status.lock().unwrap().clone();
    snapshot
}

/// 把主窗显示出来并聚焦（托盘的「显示主窗口」、以及将来任何"从后台唤回"的入口）。
fn show_main(app: &tauri::AppHandle) -> Result<(), String> {
    let w = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    // 顺序不能反：先 show 再 set_focus。窗口还藏着的时候 macOS 不给键盘焦点，
    // 只 set_focus 会得到一个「看得见但不响应键盘」的窗口。
    w.show().map_err(|e| e.to_string())?;
    let _ = w.unminimize();
    w.set_focus().map_err(|e| e.to_string())
}

/// 建托盘：菜单 = 显示主窗口 / 开始录制 / 退出。
///
/// 托盘是这个应用**唯一**的常驻入口——关窗只是隐藏（见 `on_window_event`），
/// 所以「怎么把它叫回来」「怎么在不打开窗口的情况下停止录制」必须都在这里。
/// 录制开关**不**顺手把窗口弹出来：把窗口收起来、在别处操作、再收起来，
/// 正是托盘存在的意义。
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, MENU_SHOW, "显示主窗口", true, None::<&str>)?;
    let toggle = MenuItem::with_id(app, MENU_TOGGLE_RECORD, "● 开始录制", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, MENU_QUIT, "退出 Auto Flow", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &toggle, &quit_item])?;

    let mut builder = tauri::tray::TrayIconBuilder::with_id(TRAY_ID)
        .menu(&menu)
        .tooltip("Auto Flow")
        // macOS 默认就是左键出菜单。显式写出来是为了**别**把它当默认值改掉：
        // 关掉它之后左键就成了"直接显示窗口"，而右键菜单在这个应用里没地方发现。
        .show_menu_on_left_click(true)
        .on_menu_event(|app, ev| {
            // `MenuId` 是 `pub struct MenuId(pub String)`，取 &str 只有这一条路
            // （它也有 AsRef<str>，两处都行；这里用字段访问避免再引入一个 trait 导入）。
            let id = ev.id().0.as_str();
            // 落日志：托盘点击是**界面之外**的交互，出了问题（窗口没回来 / 录制没切换）
            // 界面上没有任何痕迹可查。日志是唯一的现场。
            log_line(&format!("托盘菜单点击: {id}"));
            match id {
                MENU_SHOW => {
                    let _ = show_main(app);
                }
                MENU_TOGGLE_RECORD => {
                    // 录制状态的真源在前端 store（它同时管家里的录制缓冲），
                    // 托盘只负责"告诉它按一下"，不自己翻转状态——两边各存一份
                    // 就会出现「托盘显示录制中、界面显示已停止」。
                    let _ = app.emit("tray_action", "toggle_record");
                }
                MENU_QUIT => app.exit(0),
                _ => {}
            }
        });

    // 用应用自己的图标。**不能**标成 template：template 会把彩色图标当蒙版渲染成
    // 纯色剪影，而这张图标是有内容的（见 icons/README）。
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(tauri::image::Image::new_owned(
            icon.rgba().to_vec(),
            icon.width(),
            icon.height(),
        ));
    }
    builder.build(app)?;
    app.manage(TrayHandles { toggle });
    // 正向日志。托盘是**界面之外**的东西，建失败时用户只会觉得"图标没出现"，
    // 而"没出现"和"没建"在日志里都等于没有痕迹——所以成功也要留一行。
    // （另外：托盘的创建结果没法用产物断言验——`TrayIconBuilder::with_id` 的 id、
    //  `generate_handler!` 的命令名都会被 LLVM 内联成立即数比较，二进制里搜不到；
    //  同理 `window_bounds` / `app.shutdown` 这些老命令名也搜不到。）
    log_line("托盘已创建（显示主窗口 / 开始录制 / 退出）");
    Ok(())
}

/// 前端把录制态同步给托盘：改菜单项文案 + 工具提示 + 菜单栏上的角标。
///
/// 为什么由前端推而不是 Rust 自己判：录制态的**真源在前端 store**（它同时维护
/// 前端的事件缓冲、`lastRecordInfo` 等）。Rust 另存一份的话，两边一定会在
/// 「快捷键开始 / 后端自动停止 / 崩溃重连」这些路径上分叉，而分叉的表现是
/// 托盘写着「录制中」、界面却已经停了。
#[tauri::command]
fn af_tray_state(app: tauri::AppHandle, recording: bool) -> Result<(), String> {
    let text = if recording { "■ 停止录制" } else { "● 开始录制" };
    let menu_ok = app
        .try_state::<TrayHandles>()
        .map(|h| h.toggle.set_text(text).is_ok());
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let (tip, badge) = if recording {
            ("Auto Flow — 录制中…", Some("●"))
        } else {
            ("Auto Flow", None)
        };
        // 这三个返回值**收下来**再落日志。它们失败是静默的（原本都被 `let _ =` 吞掉），
        // 而失败的表现只有「录制中却没有角标」——那时现场能看到的只有"角标没出现"，
        // 与"图标根本没建"分不开。收下来，两者在日志里就是两行不同的话。
        let tip_ok = tray.set_tooltip(Some(tip)).is_ok();
        let badge_ok = tray.set_title(badge).is_ok();
        // 这一行是「托盘到底有没有出现在菜单栏、角标有没有设上去」唯一能**自动化**
        // 问到的地方，四段各管一件事：
        //   recording / 角标 —— 前端推来的状态、以及我们打算设的角标值
        //   set_*            —— 上面三个调用是否成功（false = 静默失败）
        //   rect             —— AppKit 给出的状态项位置，`None` 就是**没有**状态项
        // 只有 rect 的话，「图标在但角标没设上去」和「图标不在」会长得一样。
        // `rect` 用 `{:?}` 打整体：`Rect` 的 position/size 是枚举包装，打印整体
        // 免得为一行日志引入对字段形状的依赖。
        log_line(&format!(
            "托盘状态项 recording={recording} 菜单文案={text:?} set_menu={menu_ok:?} 角标={badge:?} set_title={badge_ok} set_tooltip={tip_ok} rect={:?}",
            tray.rect()
        ));
    } else {
        // 走到这里说明 `build` 成功了但句柄查不到——托盘的 id 被换过，或它已被移出。
        // 带上 recording：这一支的两侧表现（该有角标时没有 / 本不该有角标）完全不同，
        // 只说"查不到"就无法判断这次失败有没有让用户看见错的东西。
        log_line(&format!(
            "托盘状态项查不到（tray_by_id 返回 None）recording={recording}"
        ));
    }
    Ok(())
}

/// 前端主动把窗口藏起来（托盘化）。关窗走的是 `on_window_event` 那条路，
/// 这个命令是给界面上的「隐藏到托盘」按钮用的。
#[tauri::command]
fn af_hide_window(app: tauri::AppHandle) -> Result<(), String> {
    let w = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    w.hide().map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
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
            // 托盘失败不阻断启动：它只是"收起来"的入口，sidecar/画布才是主体。
            if let Err(e) = setup_tray(&handle) {
                log_line(&format!("托盘创建失败：{e}"));
            }
            spawn_and_watch(&handle, &state);
            Ok(())
        })
        .on_window_event(|window, event| {
            // 点 × = 收进托盘，**不是退出**。定时任务、录制、正在跑的回放都还在，
            // 直接退掉会让它们无声无息地断在半路。
            // 真退出只有两条路：托盘菜单「退出」、cmd+Q。
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            send_rpc,
            rpc_status,
            set_click_through,
            window_bounds,
            af_tray_state,
            af_hide_window
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        // macOS 的 Dock：窗口被收进托盘后点 Dock 图标，系统发的是 `Reopen`，
        // 而 Tauri **只把它转给我们、不会自动把窗口叫回来**。不接这一下，
        // 用户点 Dock 会"什么都没发生"——而那是找回窗口最直觉的动作
        // （比去菜单栏找托盘图标自然得多）。
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen { .. } = event {
            let _ = show_main(handle);
        }
        // 非 macOS 上没有 Reopen；把 handle 用掉避免 unused 警告
        #[cfg(not(target_os = "macos"))]
        {
            let _ = handle;
        }
    });
}
