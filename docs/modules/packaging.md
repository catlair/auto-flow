# 打包 / 签名 / 部署

> 状态：✅
> 一句话：Python sidecar（PyInstaller onedir）+ Tauri .app 的双签构建链与
> /Applications 固定路径部署。

## 代码位置

- `scripts/build_sidecar.sh` — PyInstaller onedir + MacDev 签名 + 冒烟
- `scripts/sign_tauri_app.sh` — sidecar/.app 双签（强化运行时+库校验豁免）+ 启动冒烟
- `scripts/sync_app.sh` — 旧版移废纸篓 → 拷贝 /Applications → 去 quarantine → 启动
- `scripts/make_dmg.sh` — 签名后自建 DMG（不用 Tauri 内置 dmg，顺序对不上）
- `tauri/src-tauri/entitlements-sidecar.plist` — sidecar 库校验豁免

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-PKG-01 | sidecar 打包 | ✅ | onedir 固定路径，禁 onefile（随机 _MEI 毁 TCC） |
| F-PKG-02 | 双签 | ✅ | sidecar（带 entitlements）→ .app 主体，均 runtime+timestamp |
| F-PKG-03 | sidecar 冒烟 | ✅ | 签名脚本实发 app.info 取首行，防「签名有效但起不来」 |
| F-PKG-04 | /Applications 部署 | ✅ | 固定路径保 TCC 授权；旧版进废纸篓可反悔 |
| F-PKG-05 | DMG 分发 | ✅ | 签名后打包；公证可选（需 Apple 凭证） |
| F-PKG-06 | 诊断文件 | ✅ | `--check` 写 Finder 环境真实 AX 状态到数据目录 |

## 验收记录

- **F-PKG-03**（2026-09-05 起每轮构建）：冒烟输出 app.info JSON 且权限三项可见。
- **F-PKG-04**（2026-09-05）：部署后真机启动、授权横幅消失、侧栏授权一次跨构建有效。
- **F-PKG-01**（2026-09-12）：卸载 PySide6 后 sidecar 233MB（Qt 本就不在 sidecar
  依赖里；体积大头是 opencv/onnxruntime，可选包化在计划中）。

## 设计要点

1. **强化运行时下的库校验豁免**：sidecar 必须带 entitlements
   （`com.apple.security.cs.disable-library-validation`）——PyInstaller 的
   _internal/*.dylib 是 ad-hoc 签名，强化运行时默认拒绝跨 Team 加载，
   表现为 sidecar 静默起不来、界面权限全 ✗（codesign verify 却显示 valid）。
   所以签名脚本最后必须实发一帧 RPC 冒烟。
2. **签名顺序**：先递归签 sidecar（runtime+timestamp+entitlements），再签 .app
   主体（不带 --deep）；DMG 必须在签名之后打。
3. **固定路径**：/Applications + MacDev（自签 CA）→ designated requirement 稳定，
   辅助功能/输入监控授权跨构建有效。dist/ 里反复重建会产生失效的 TCC 条目，
   系统设置里可能出现多个同名 app——勾旧的无效。
4. **旧版进废纸篓**：sync_app 用 mv 到 ~/.Trash 而非 rm -rf，防路径写错不可恢复。
5. **⚠️ 修完必须部署**：代码修复不重跑 build+sync 不生效——曾致 /Applications
   旧包带病运行 6 天（用户报的「运行中按钮不变化、F10 不能停止」即旧包问题）。
6. **pynput Controller 在 frozen sidecar 里静默失效**：press/release 不抛异常但
   事件不出现——回放引擎的鼠标输出必须用 Quartz 直发（kCGSessionEventTap）。
   键盘 Controller 同样有静默丢失风险（中文已改 CGEvent Unicode 通道）。

## 已知问题

- 未公证：分发到其他机器需手动右键打开或补 notarization（脚本已留参数位）。
- sidecar onedir 目录名曾与 .gitignore 规则不一致导致嵌套错位（已统一）。

## 变更记录

- 2026-09-05 Tauri 双签链路 + 冒烟（外部 AI 贡献轮）
- 2026-09-12 删除 PySide6 旧打包链（build_app.sh / main.py / ui/）
