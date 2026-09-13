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

## 已知问题

- **模板/截屏密度不一致 = 静默失败**（2026-09-13 定位并修复，记录备查）：
  现象是「截图识别没有效果」——节点一直按「找不到」跳过、日志里什么都没有。
  真凶是 mss 默认请求名义分辨率：Retina 屏 `scale=1.0`（1280×832），而
  `screencapture -i` 截出的模板是 2x（144dpi）。实测同一目标：
  2x 模板对 1x 屏 = **0.53**（低于默认阈值 0.8），缩到同密度 = **0.9998**，
  2x 模板对 2x 屏 = **1.0**。已由 `_prefer_physical_resolution()` 修复；
  若今后有人手工塞进 1x 小图（如别处下载的图标），`_warn_density_mismatch`
  会点名告警。
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
