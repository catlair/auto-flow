# macOS 权限

> 状态：✅
> 一句话：辅助功能 / 输入监控 / 屏幕录制三项权限的检测、引导与真实可收性自检。

## 代码位置

- `core/permissions.py` — check_accessibility / check_input_monitoring /
  check_screen_recording（均含 _retry_call 防 pyobjc 懒加载 KeyError）
- `rpc/server.py` — permission.changed 2s 轮询、app.openPermissionSettings、
  app.requestPermissions
- `tauri/src/components/PermissionBanner.vue` — 横幅 + 自检按钮
- `docs/权限引导.md` — 用户侧授权手册

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-PRM-01 | 辅助功能检测 | ✅ | AXIsProcessTrusted（prompt 可弹系统提示） |
| F-PRM-02 | 输入监控检测 | ✅ | CGPreflightTapRequestsAreNonUserApproved |
| F-PRM-03 | 屏幕录制检测 | ✅ | CGPreflightScreenCaptureAccess + 主动请求 |
| F-PRM-04 | 权限变化推送 | ✅ | 后端 2s 轮询，变化才发 permission.changed |
| F-PRM-05 | 真实可收性自检 | ✅ | F18 回环（hotkeys.md F-HOT-07） |
| F-PRM-06 | 未授权横幅 | ✅ | 快照/自检失败都引导到系统设置 |

## 验收记录

- **F-PRM-01**（2026-08-30）：未授权时启动弹引导、授权后横幅自动消失（真机）。
- **F-PRM-06**（2026-09-12）：`--check` 模式实测 Finder 环境下 ax_trusted=false
  （授权未落到新二进制），引导重授权后转 true。

## 设计要点

1. **TCC 按二进制（责任进程）授权**：这是本项目最大的权限坑——
   - sidecar 直跑时，CGEventTap 可能**创建成功但事件静默不投递**（未授权），
     预检 API 仍报可用 → 必须用 F18 回环自检分辨。
   - 重新构建二进制后，若签名身份（MacDev）与安装路径（/Applications）稳定，
     授权可跨构建保持；dist/ 下反复重建会让系统设置堆积失效条目。
   - 曾出现「用户已勾选但仍弹授权」：系统设置里存在多个旧条目，勾的是旧的。
2. **权限快照不可全信**：accessibility/inputMonitoring/screenRecording 三项
   快照可能全 true 而事件收不到（见上），快照只作引导，真实状态以自检为准。
3. **_retry_call**：pyobjc 懒加载首访符号偶发 KeyError/AttributeError，
   重试一次即命中缓存。
4. **sidecar 启动写权限快照日志**：外壳拉起 vs 终端直跑的责任进程不同，
   读到的授权状态可能不同——排查「横幅全 ✗ 但 sidecar 活着」必须有这份记录。

## 已知问题

- 输入监控没有 prompt API，只能引导用户手动添加。
- 快照轮询 2s 粒度，授权后横幅最多延迟 2s 消失。

## 变更记录

- 2026-09-05 --check 诊断模式 + 横幅自动重检
- 2026-09-12 F18 回环自检并入横幅
