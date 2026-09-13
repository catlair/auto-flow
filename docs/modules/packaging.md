# 打包 / 签名 / 部署

> 状态：✅
> 一句话：Python sidecar（PyInstaller onedir）+ Tauri .app 的双签构建链与
> /Applications 固定路径部署。

## 代码位置

- `scripts/build_sidecar.sh` — PyInstaller onedir + MacDev 签名 + 冒烟
- `scripts/sign_tauri_app.sh` — sidecar/.app 双签（强化运行时+库校验豁免）+ 启动冒烟
- `scripts/sync_app.sh` — 旧版移废纸篓 → 拷贝 /Applications → 去 quarantine → 启动
- `scripts/check_installed.py` — **部署后产物校验**（读 frozen PYZ + 真起 sidecar）
- `scripts/clean_old_builds.sh` — 构建残留清理（默认预演，`--apply` 才动手）
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
| F-PKG-07 | **部署后产物校验** | ✅ | `check_installed.py`：产物级断言（frozen PYZ 反编译）+ RPC 级断言，收「改了代码却没变化」的口 |
| F-PKG-08 | 残留清理 | ✅ | `clean_old_builds.sh`：防批量删除保护产生的 `.old.*` 堆积，每构建一轮约 +470MB |

## 验收记录

- **F-PKG-03**（2026-09-05 起每轮构建）：冒烟输出 app.info JSON 且权限三项可见。
- **F-PKG-04**（2026-09-05）：部署后真机启动、授权横幅消失、侧栏授权一次跨构建有效。
- **F-PKG-01**（2026-09-12）：卸载 PySide6 后 sidecar 233MB（Qt 本就不在 sidecar
  依赖里；体积大头是 opencv/onnxruntime，可选包化在计划中）。
- **F-PKG-07**（2026-09-13）：11 项断言全过——先对**旧安装版**反向验证出
  「keysToText 未进包 / record_stop 未做元素级快照」（证明断言有区分度），
  再对新装版全绿。
- **F-PKG-07**（2026-09-13 下午，视觉密度修复）：新增产物级断言
  「core.vision 去掉 mss 的 NominalResolution」。先在**旧安装版**上跑出
  该条 ✗（其余 11 项 ✓，说明断言有区分度而非打包整体有问题），签名部署后
  12 项全绿。另做**安装后**端到端验证：以 `condition`(图像存在) 门控 `delay`
  节点跑真工作流——模板在屏上 → `['condition','delay']`；换一张不在屏上的
  模板 → `['condition']`（两向都对，证明门控不是恒真/恒假）。

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
   光看构建成功或 `.app` 修改时间**判断不出来**：只跑 `tauri build` 不跑
   `build_sidecar.sh`，包里就是上一次的 Python 代码。所以流水线第 5 步是
   `check_installed.py`，它打开产物本身验证（见要点 7）。
6. **pynput Controller 在 frozen sidecar 里静默失效**：press/release 不抛异常但
   事件不出现——回放引擎的鼠标输出必须用 Quartz 直发（kCGSessionEventTap）。
   键盘 Controller 同样有静默丢失风险（中文已改 CGEvent Unicode 通道）。
7. **产物级校验的取法**（`check_installed.py`）：
   - `core.*` / `rpc.controller` 在 **PYZ** 里（`ZlibArchiveReader.extract`
     直接返回 code object，不是 bytes）；
   - **入口脚本 `rpc/server.py` 不在 PYZ 里**，它是 CArchive 项名 `server` 存的
     marshal 字节，要 `marshal.loads(CArchiveReader(exe).extract("server"))`；
   - 判断「某函数里还有没有 `pop`」要取**该函数自身**的 code object 看
     `co_names`，不能扫全模块（模块里别处可能合法地 pop）；
   - `record.stop` 把统计**直接当 result 返回**，不嵌套在 `stats` 里。
8. **残留清理的跨卷坑**：项目在独立 APFS 卷上时，`mv` 到 `~/.Trash` 可能是跨设备
   rename，报 `EXDEV` 后一份都移不走；Finder/AppleScript 删除会被宿主沙箱
   以「权限违例」挡掉。`clean_old_builds.sh` 因此在 `mv` 失败时退化为直接删除
   （`.old.*` 全是可再生构建产物）。dist/ 里的一次性遗留（旧版 .app/.dmg）
   不带 `.old` 后缀，为防误删不纳入脚本，需手动清。
   **2026-09-13 实测补充**：在 `/Users/catlair/mobile` 这个卷上跑 `--apply`，
   4 份残留（约 0.5GB）**全部成功移入 `~/.Trash`**，没走删除兜底。所以这条路径
   至少现在是通的、可反悔的——清理前不必预设「不可恢复」。

## 已知问题

- 未公证：分发到其他机器需手动右键打开或补 notarization（脚本已留参数位，
  需 `APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID`）。
- sidecar onedir 目录名曾与 .gitignore 规则不一致导致嵌套错位（已统一）。
- dist/ 里可能残留一次性旧产物（不带 `.old` 后缀，脚本覆盖不到），偶发需手动清。

## 变更记录

- 2026-09-05 Tauri 双签链路 + 冒烟（外部 AI 贡献轮）
- 2026-09-12 删除 PySide6 旧打包链（build_app.sh / main.py / ui/）
- 2026-09-12 残留清理脚本（防批量删除保护产生的堆积）
- 2026-09-13 **部署后产物校验** `check_installed.py`：产物级 + RPC 级两层断言，
  收「改了代码却没变化」的口；同日清理构建残留（脚本原先担心跨卷 `mv` 到废纸篓
  会报 EXDEV，实测 4 项**都成功移入 `~/.Trash`**，担心不成立，见 §8）
- 2026-09-13 产物级断言补 `core.vision`（截屏密度修复），12 项；补
  「安装后端到端跑一次真工作流」的验证手法（condition 门控 delay，
  不需要点鼠标，可在用户机器上安全执行）
- 2026-09-13 产物级断言再补一条（模板密度自动对齐），13 项
