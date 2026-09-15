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
| F-PKG-07 | **部署后产物校验** | ✅ | `check_installed.py`：产物级断言（frozen PYZ 反编译）+ RPC 级断言（含**真跑一个必定触发后端告警的条件节点**，验证 `log.warning` 真的送达），收「改了代码却没变化」的口 |
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
  > ⚠️ **这条验收手法在 v4 已失效**：条件节点不再门控后续节点，门控语义搬到了
  > 边上（见 [flow.md](flow.md)）。v4 下等价的验证是「把 `condition` 的
  > `true` 接到 delay、`false` 接到另一个节点，断言只走 `true` 那条」。
  > 保留原记录只为说明当时的做法，不要照它复现。
- **F-PKG-07**（2026-09-13 傍晚，后端告警转发）：新增 3 项（2 产物级 + 1 RPC 级，
  共 16 项）。仍走同一套反向验证：先在**旧安装版**上跑，新加的 3 项全 ✗ 而原 13 项
  全 ✓（区分度确认，不是打包整体坏）；再对**源码树** sidecar 单跑 RPC 级，
  拿到 `log.warning`（`core.vision: 模板图读不出来…`）。触发手法特意选了
  「条件节点指向不存在的模板」——`find_template` 在读屏之前就返回，
  不碰键鼠、不依赖屏幕录制权限，可在用户机器上安全执行。
- **F-PKG-07**（2026-09-14，v4 流程图）：新增 7 项产物级 + 11 项 RPC 级
  （合计 17 产物 + 17 RPC = 34 项）。反向验证做了**两次**，因为这次改动的
  失败模式是「静默按旧模型执行」而不是报错：
  1. 对**旧安装版**跑全套 → 新增的 17 项全 ✗（其中 `edge.remove` 一条当时
     是**假通过**，见下方教训，修好后同样是 ✗）、原有 16 项全 ✓
     （区分度确认，不是打包整体坏）。
  2. 对**源码树**编译出的 code object 跑同一批谓词 → 17 项产物级全 ✓
     （证明断言不是恒假——新写的断言「在正确的输入上也通过」必须单独验一次，
     否则一个写错的断言看起来和「改动没进包」一模一样）。
  新增断言里最关键的一条是 `core.events.SCRIPT_VERSION == 4`：打进来的若是
  上一版 sidecar，打开 v4 文件**不报任何错**，会退化成「按 nodes 列表顺序跑」——
  界面看着正常、执行顺序却是错的。RPC 级还覆盖了自环被拒（-32602）、
  `edge.remove` 按 `(src,port)` 删、`workflow.setStart` 生效、
  以及对**不存在的 uid** 改坐标必须报错（-32602）。
  ⚠️ 其中 `edge.remove` 那条**第一版写成只看剩余边数**，在老 sidecar 上
  返回 `-32601` 时结果为 0 条 → **假通过**；改成同时要求「没报错」才修好。
  这条教训通用：**断言「集合为空」时必须一并断言「调用本身成功」**。
  ⚠️ **第二次踩坑（同一批断言，脚本自身的 bug）**：三条「改动是否生效」的断言
  （`node.setPos` / `edge.remove` / `workflow.setStart`）**一起假失败**，
  症状是「坐标没写进去、边一条没删、start 还是 None」——看起来完全像后端坏了。
  真因是脚本执行顺序：第 (2) 步为了验迁移把当前工作流**换成了 v3 那个**，
  它的节点 uid 是 `n0/n1/n2`，而第 (3) 步全部用 `a`/`b` 寻址，
  `_node_by_uid` 老老实实抛了「节点不存在」。在第 (3) 步开头补一次
  `workflow.load(v4)` 即全绿。
  教训：**一串断言共享同一份服务端状态时，任何一步换了状态都要显式换回来**；
  另外「一批断言同时失败且症状相同」通常指向**共同的输入错了**，
  而不是三个独立的功能一起坏——先怀疑 fixture，别急着改实现。

- **F-PKG-07**（2026-09-15，节点改动改按 uid 寻址）：新增 1 项产物级 + 3 项 RPC 级
  （合计 18 产物 + 20 RPC = 38 项）。这次的反向验证**直接在真安装版上复现了 bug**，
  比「断言变红」更有说服力：
  1. 对**尚未重打的旧安装版**跑全套 → 只有新增的 4 项 ✗，原有 34 项全 ✓。
     其中 `node.remove` 那条的失败信息是「剩下 `['b']`」——脚本故意送
     `{"index": 0, "uid": "b"}`，老 sidecar 只认下标，**照下标 0 把 a 删了**，
     正是要修的那个「删错节点」；`start` 也因此没被清空（活下来的不是起点）。
     顺带确认「不存在的 uid」在老 sidecar 上**返回 None 而不是报错**。
  2. 重打部署后 → 38 项全绿。
  这条也说明「产物级断言只能证明代码进去了，行为要靠 RPC 级实测」：
  产物级那条 `_index_of` 只是符号存在性，真正证明 uid 生效的是 RPC 级那三条。

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
   - **要收通知就必须用读线程**：通知与响应在同一条 stdout 上交错，同步
     `readline` 找 id 时会顺手把夹在中间的通知读掉、丢掉——`log.warning`
     那条断言会因此**误报失败**（看起来像后端没转发，其实是收尾脚本自己吞了）。
     现在 `Sidecar` 用后台线程把两者分流到 `_responses` / `notifications`。
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
- 2026-09-13 断言补到 16 项：`_UiLogHandler` / 防递归守卫 / `log.warning` 端到端投递；
  `check_installed.py` 的 sidecar 客户端改为**读线程分流响应与通知**
  （原同步 `readline` 会把交错的通知吞掉，收不到就误报失败）
- 2026-09-14 断言补到 34 项（17 产物级 + 17 RPC 级）覆盖 v4 流程图：`SCRIPT_VERSION`、
  出口名常量、`_linear_positions`、`MAX_STEPS`、图遍历函数名、画布方法表，
  以及端到端「v4 文件边不丢 / v3 文件迁移后坐标已摆开 / 自环被拒 / 起止节点可设 /
  不存在的 uid 改坐标必须报错」。
  同时标注「condition 门控 delay」那条旧验收手法**在 v4 已失效**。
  同轮修掉脚本自身一个 bug：第 (3) 步沿用了第 (2) 步留下的 v3 工作流（uid 是
  `n0/n1/n2`）却用 `a`/`b` 寻址，导致三条「改动是否生效」的断言一起假失败
  （详见「验收记录」里的第二次踩坑）
- 2026-09-15 断言补到 38 项（18 产物级 + 20 RPC 级）：节点改动改按 uid 寻址，
  新增「故意送错下标 + 正确 uid」的端到端断言（老 sidecar 上会照下标删错节点，
  已在真安装版上复现）
- 2026-09-15 断言补到 42 项（20 产物级 + 22 RPC 级）：边的落点侧 `dst_side`。
  产物级判 `core.events` 里 `left/top/bottom` 在、`right` **不在**、且
  `normalize_target_side` 在；RPC 级实测 `edge.add` 真把 `dst_side` 存下来、
  脏值（含 `right`）收敛到 `left`。这一组正是「不生效也不报错」——旧 sidecar
  会**静默忽略** `dst_side`，画布照默认侧画，从外面看和「用户自己连到左边」一样。
  同轮踩到并记录了「目视验收用了旧 sidecar 导致假失败」，见 `frontend.md` 验收记录
