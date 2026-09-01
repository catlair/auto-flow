// 仅作 Tauri 2 入口；实际逻辑在 lib.rs 的 run()。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    autoflow_tauri_lib::run()
}
