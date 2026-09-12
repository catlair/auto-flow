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

## 验收记录

- **F-VIS-03/02**（2026-09-05）：屏上截块即找，score 1.0 中心零偏差；静态壁纸
  模板连续匹配位置一致；Retina 2x 下点击坐标与逻辑点对齐。
- **F-VIS-05**（2026-09-05）：屏上渲染「AutoFlow目标文字42」，OCR 中心点误差 2px。
- **F-VIS-06**（2026-08-31）：虚拟屏（2560×1664@2x 注入截图）检测 bus/person，
  点击落点与检测框中心一致；`test_broadcast_summary_never_mutates_tree` 同文件。
- **F-VIS-01**（2026-08-31）：显示器休眠（mss monitors 空）时 Quartz 路径自动接管。

## 设计要点

1. **坐标空间**：截屏为物理像素，点击为逻辑点。scale = 截屏宽 / 主屏逻辑宽，
   所有查找结果除以 scale 再交给点击层。
2. **mss → Quartz 兜底**：显示器休眠/锁屏时 mss 枚举不到显示器（monitors 仅剩
   聚合项）；Quartz `CGWindowListCreateImage` 仍可截（锁屏时截到锁屏本身，属预期）。
3. **OCR 输入必须 PNG 编码**：原始 BGR 字节直接喂 CGImageSource 会静默解码失败
   返回空（曾致 OCR 全空，排查半天）。
4. **pyobjc API 签名**：`CGWindowListCreateImage` 需 4 参（含 imageOption）；
   PNG fileType 用裸值 4（`NSBitmapImageRep.NSPNGFileType` 在部分 pyobjc 版本缺失）。
5. **YOLO 输出解析**：标准 v8/v11 导出 [1,4+nc,N]；首维小于次维时转置。
   NMS 后逆 letterbox：注意放置偏移用 `(640-nw)//2` 且逆变换减同一偏移——
   曾把 x1y1wh 当 cxcywh 解包 + pad 减半错位，框整体飞偏。
6. **默认模型播种**：打包后首次调用 `paths.default_model()` 把 Resources 里的
   yolo11n.onnx 拷到用户数据目录；自定义类别放模型同名 .txt。

## 已知问题

- 模板/背景均取自动态内容时匹配不稳定（测试曾踩），选静态区域做模板。
- 显示器锁屏时截屏拿到的是锁屏画面，视觉节点会「认真匹配锁屏」——
  自动化场景应避免锁屏运行。

## 变更记录

- 2026-08-31 图像匹配 + YOLO 落地
- 2026-09-05 OCR 落地；mss→Quartz 兜底
