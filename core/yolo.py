"""YOLO 目标检测：onnxruntime（CoreML/CPU），标准 YOLOv8/v11 检测模型。

模型文件由用户提供（.onnx，如 yolov8n.onnx）；输出格式 [1, 4+nc, N]，
坐标经 letterbox 逆变换映射回截屏像素，再除以缩放比得到逻辑坐标（点）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

_ENGINES: dict[str, "YoloEngine"] = {}


def get_engine(model_path: str) -> "YoloEngine":
    """按模型路径缓存引擎，避免重复加载。"""
    key = os.path.abspath(model_path)
    if key not in _ENGINES:
        _ENGINES[key] = YoloEngine(key)
    return _ENGINES[key]


@dataclass
class Detection:
    label: str
    confidence: float
    x: int            # 逻辑坐标（点）中心
    y: int
    w: int            # 逻辑尺寸
    h: int


class YoloEngine:
    def __init__(self, model_path: str, imgsz: int = 640) -> None:
        import onnxruntime as ort
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"YOLO 模型不存在: {model_path}")
        self.imgsz = imgsz
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        try:
            self.session = ort.InferenceSession(model_path, so, providers=providers)
        except Exception:
            self.session = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.labels = self._load_labels(model_path)

    @staticmethod
    def _load_labels(model_path: str) -> list[str]:
        """同名 .txt（每行一个类别名）优先，否则 COCO 80 类。"""
        txt = os.path.splitext(model_path)[0] + ".txt"
        if os.path.exists(txt):
            with open(txt, "r", encoding="utf-8") as f:
                names = [line.strip() for line in f if line.strip()]
            if names:
                return names
        return COCO_LABELS

    # ---- 推理 ----
    def detect_bgr(self, bgr: np.ndarray, scale: float,
                   conf_threshold: float = 0.5) -> list[Detection]:
        img, ratio, (pad_x, pad_y) = self._letterbox(bgr)
        blob = img.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = np.asarray(self.session.run(None, {self.input_name: blob})[0])
        preds = out[0]                      # [4+nc, N] 或 [N, 4+nc]
        if preds.shape[0] < preds.shape[1]:  # [4+nc, N] → [N, 4+nc]
            preds = preds.T
        xy = preds[:, :4]                   # cxcywh（letterbox 640 空间）
        scores = preds[:, 4:]
        cls_ids = scores.argmax(axis=1)
        confs = scores.max(axis=1)
        keep = confs >= conf_threshold
        xy, confs, cls_ids = xy[keep], confs[keep], cls_ids[keep]
        if len(xy) == 0:
            return []
        rects = np.stack([xy[:, 0] - xy[:, 2] / 2, xy[:, 1] - xy[:, 3] / 2,
                          xy[:, 2], xy[:, 3]], axis=1)
        idxs = cv2.dnn.NMSBoxes(
            [r.tolist() for r in rects], confs.tolist(), conf_threshold, 0.45)
        dets: list[Detection] = []
        H, W = bgr.shape[:2]
        for i in np.array(idxs).flatten():
            x1, y1, bw_l, bh_l = rects[i]        # letterbox 空间 x1y1wh
            px1 = max((x1 - pad_x) / ratio, 0.0)
            py1 = max((y1 - pad_y) / ratio, 0.0)
            pw = min(bw_l / ratio, W - px1)
            ph = min(bh_l / ratio, H - py1)
            if pw <= 0 or ph <= 0:
                continue
            label = self.labels[cls_ids[i]] if cls_ids[i] < len(self.labels) else str(cls_ids[i])
            dets.append(Detection(
                label=label, confidence=float(confs[i]),
                x=int((px1 + pw / 2) / scale), y=int((py1 + ph / 2) / scale),
                w=int(pw / scale), h=int(ph / scale)))
        dets.sort(key=lambda d: -d.confidence)
        return dets

    @staticmethod
    def _letterbox(img: np.ndarray, color=(114, 114, 114)):
        """缩放+填充到 640²；返回 (图, 缩放比, (left, top) 内容偏移)。"""
        h, w = img.shape[:2]
        s = min(640 / h, 640 / w)
        nw, nh = int(round(w * s)), int(round(h * s))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        left, top = (640 - nw) // 2, (640 - nh) // 2
        out = np.full((640, 640, 3), color, dtype=np.uint8)
        out[top:top + nh, left:left + nw] = resized
        return out, s, (left, top)
