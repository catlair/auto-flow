# 视觉：截屏 / 模板匹配 / OCR / YOLO

> 状态：✅
> 一句话：屏幕捕获与三类目标查找（图像模板 / OCR 文字 / YOLO 目标），
> 供点击节点与条件判断共用。

## 代码位置

- `core/vision.py` — grab_screen_bgr（mss + Quartz 兜底）、find_template
- `core/ocr.py` — Vision 框架 OCR（find_text / recognize_texts）
- `core/yolo.py` — YoloEngine（onnxruntime，letterbox/NMS/坐标换算）
- `models/yolo11n.onnx` — 默认模型（COCO 80 类）

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-VIS-01 | 全屏/区域截屏 | ✅ | mss 优先，锁屏/休眠时 Quartz 兜底 |
| F-VIS-02 | Retina 坐标换算 | ✅ | 物理像素 ÷ scale → 逻辑点（与点击层一致） |
| F-VIS-03 | 模板匹配 | ✅ | cv2.matchTemplate TM_CCOEFF_NORMED |
| F-VIS-04 | 截取模板 | ✅ | `screencapture -i` 框选 → template.snipped 通知回填 |
| F-VIS-05 | OCR 找文字 | ✅ | Vision VNRecognizeTextRequest，PNG 编码后喂帧 |
| F-VIS-06 | YOLO 检测 | ✅ | CoreML 加速，置信度/命中序号/类别过滤 |
| F-VIS-07 | 找不到策略 | ✅ | 超时重试；跳过或停止工作流 |
| F-VIS-08 | 模板/截屏同密度 | ✅ | 截屏取物理像素（去掉 mss 的 NominalResolution），与 screencapture 模板对齐 |
| F-VIS-09 | 「找不到」可诊断 | ✅ | 模板文件失效 / 纯色模板 / 密度不一致，各留一次告警 |
| F-VIS-10 | 模板密度自动对齐 | ✅ | 按「屏幕 scale ÷ 模板密度」重采样模板（带缓存），跨密度组改分辨率后**不必重截模板** |

## 验收记录

- **F-VIS-03/02**（2026-09-05）：屏上截块即找，score 1.0 中心零偏差；静态壁纸
  模板连续匹配位置一致；Retina 2x 下点击坐标与逻辑点对齐。
- **F-VIS-05**（2026-09-05）：屏上渲染「AutoFlow目标文字42」，OCR 中心点误差 2px。
- **F-VIS-06**（2026-08-31）：虚拟屏（2560×1664@2x 注入截图）检测 bus/person，
  点击落点与检测框中心一致；`test_broadcast_summary_never_mutates_tree` 同文件。
- **F-VIS-01**（2026-08-31）：显示器休眠（mss monitors 空）时 Quartz 路径自动接管。
- **F-VIS-08**（2026-09-13，真机 Retina 2x）：截屏 `(1664, 2560, 3) scale=2.0`；
  从截屏裁 240×120 模板 → `found=True score=1.0000`，全局逻辑坐标误差 `(0, 0)`。
  回归测试 `test_mss_asks_for_physical_resolution`（去掉物理像素修复即失败）。
- **F-VIS-09**（2026-09-13）：`test_density_mismatch_is_reported_once` /
  `test_missing_template_file_is_reported_once` / `test_flat_template_is_reported`
  （三处告警逐个关掉即失败）；配对用例 `test_no_density_warning_when_densities_agree`
  守住误报。
- **F-VIS-10**（2026-09-13，真机切显示模式）：模板 38×44@144dpi（2x）切到 1.0x 组
  （2560×1664，`scale=1.0`）后 `found=True score=0.9355 命中=(28,27) adjusted=0.5`，
  命中位置经**放大截图肉眼确认**在目标中心；切回 2.0x 组 `score=0.9590` 与不重采样
  **完全一致（无回归）**。回归测试 `test_density_mismatch_is_auto_resampled_and_matches`
  及其对照 `test_without_alignment_a_2x_template_would_miss`（关掉密度识别即失败）。

## 设计要点

1. **坐标空间**：截屏为物理像素，点击为逻辑点。`scale = 截屏宽 / 该屏逻辑宽`，
   所有查找结果除以 scale 再交给点击层。**分母必须用被截那块屏自己的宽度**——
   混合 Retina/非 Retina 双屏下两块屏比例不同，用主屏宽度会整体点偏。
2. **模板与截屏必须同像素密度**（2026-09-13 修）：截屏取物理像素，而「截取模板」
   用的 `screencapture` 产出的也是物理像素，两者天然对齐。但 mss 10.2 默认会请求
   `kCGWindowImageNominalResolution`（名义分辨率），Retina 上拿回的是**逻辑尺寸**
   （物理的一半），模板于是比屏上目标大一倍、分数整体砍半。见已知问题第一条。
3. **mss → Quartz 兜底**：显示器休眠/锁屏时 mss 枚举不到显示器（monitors 仅剩
   聚合项）；Quartz `CGWindowListCreateImage` 仍可截（锁屏时截到锁屏本身，属预期）。
4. **OCR 输入必须 PNG 编码**：原始 BGR 字节直接喂 CGImageSource 会静默解码失败
   返回空（曾致 OCR 全空，排查半天）。
5. **pyobjc API 签名**：`CGWindowListCreateImage` 需 4 参（含 imageOption）；
   PNG fileType 用裸值 4（`NSBitmapImageRep.NSPNGFileType` 在部分 pyobjc 版本缺失）。
6. **YOLO 输出解析**：标准 v8/v11 导出 [1,4+nc,N]；首维小于次维时转置。
   NMS 后逆 letterbox：注意放置偏移用 `(640-nw)//2` 且逆变换减同一偏移——
   曾把 x1y1wh 当 cxcywh 解包 + pad 减半错位，框整体飞偏。
7. **默认模型播种**：打包后首次调用 `paths.default_model()` 把 Resources 里的
   yolo11n.onnx 拷到用户数据目录；自定义类别放模型同名 .txt。
8. **「找不到」必须留痕**：模板文件失效 / 纯色模板 / 密度不一致，在返回值上与
   「屏上确实没有」**完全一样**，不记日志就等于没法排查。统一走 `_warn_once(key,…)`
   闸门（重试循环每 400ms 一轮，不设闸门会刷屏）。
9. **密度不一致要自动对齐，而不是只告警**（F-VIS-10）：匹配前按
   `屏幕 scale / 模板密度` 把模板缩放到该屏密度再匹配，缩放系数记在
   `MatchResult.adjusted`。三个实现细节都是有理由的：
   - **只对文件模板做**——模板密度只有 PNG 的 `pHYs` 块能告诉我们，内存模板
     （`template_bgr`）无从得知，调用方自己保证密度一致。
   - **必须缓存**（`_resampled`，键 `(路径, 系数)`）——匹配在重试循环里每 400ms
     一轮，每轮都 resize 是纯浪费；系数只有「屏幕密度 ÷ 模板密度」几种取值，
     命中率接近 100%。
   - **密度要四舍五入到两位**——`pHYs` 只能存整数「像素/米」，144dpi 实存
     5669 ppm，反算是 143.9926 → 1.9999。不取整的话缩放系数是 0.5000257 而非
     0.5，`INTER_AREA` 会取到非整数采样格、把细节糊掉（实测分数从 1.0 掉到 0.33）。
   - 缩放后仍不达阈值才算「真的没找到」，此时 `MatchResult.reason` 点名密度不一致。

## 已知问题

- **模板/截屏密度不一致 = 静默失败**（2026-09-13 定位并修复，记录备查）：
  现象是「截图识别没有效果」——节点一直按「找不到」跳过、日志里什么都没有。
  真凶是 mss 默认请求名义分辨率：Retina 屏 `scale=1.0`（1280×832），而
  `screencapture -i` 截出的模板是 2x（144dpi）。实测同一目标：
  2x 模板对 1x 屏 = **0.53**（低于默认阈值 0.8），缩到同密度 = **0.9998**，
  2x 模板对 2x 屏 = **1.0**。已由 `_prefer_physical_resolution()` 修复。
- **跨密度组改分辨率曾会「点错地方」**（2026-09-13 真机实测，已由 F-VIS-10 修）：
  屏幕从 2.0x 组切到 1.0x 组后，2x 模板与屏幕差一倍密度，最高分 **0.7738**，
  而且落在**错误位置**（屏幕中部 `(711,758)`）。危险之处在于它只比默认阈值 0.8
  低一点点，而置信度是用户可下调的（`confidence` 参数 `min_value=0.1`，
  「点击图片」节点 `min_value=0.05`）——调到 ≤0.77 就会**静默点错位置**，
  比「找不到」更难发现。现已自动缩放对齐，同场景变成 `True 0.9355` 且位置正确。
  仍未做的是：若有人手工塞进密度与屏幕差得更远的图（如 1x 图配 3x 屏），
  自动缩放只保证「不会静默点错」，不保证一定匹配得上。
- 模板/背景均取自动态内容时匹配不稳定（测试曾踩），选静态区域做模板。
- 显示器锁屏时截屏拿到的是锁屏画面，视觉节点会「认真匹配锁屏」——
  自动化场景应避免锁屏运行。
- 密度修正后匹配在原生分辨率（Retina 上 4 倍像素）上进行，`matchTemplate`
  耗时随之上升；双屏 + 400ms 重试间隔下实测仍有余量，未做降采样优化。

## 变更记录

- 2026-08-31 图像匹配 + YOLO 落地
- 2026-09-05 OCR 落地；mss→Quartz 兜底
- 2026-09-13 截屏改回物理像素，与 screencapture 模板同密度（修「截图识别没有
  效果」）；「找不到」的三种静默成因各补一次告警；补 7 个确定性用例
- 2026-09-13 模板密度自动对齐（F-VIS-10）：按「屏幕 scale ÷ 模板密度」重采样
  模板并缓存，跨密度组改分辨率后不必重截模板；密度取整到两位以保证缩放系数
  精确；补 7 个用例（含反向对照）。测试 141 → 148
