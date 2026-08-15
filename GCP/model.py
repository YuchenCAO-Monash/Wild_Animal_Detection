from pathlib import Path
from PIL import Image
import torch
import torchvision.transforms as transforms
import numpy as np
import cv2
from megadetector.detection import run_detector_batch
import tempfile
import os
import base64

# ---------------------------
# 基础路径和模型
# ---------------------------
BASE_DIR = Path(__file__).parent
MD_MODEL_PATH = BASE_DIR / "mdv5a.pt"
SPECIES_MODEL_PATH = BASE_DIR / "model.pt"

# 类别
CLASSES = [line.strip() for line in open(BASE_DIR / "labels.txt")]

# 设备
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# 模型加载
species_model = torch.load(SPECIES_MODEL_PATH, map_location=DEVICE, weights_only=False)
species_model.eval()
species_model.to(DEVICE)

transform = transforms.Compose([
    transforms.Resize((480, 480)),
    transforms.ToTensor()
])

# ---------------------------
# 工具函数
# ---------------------------
def get_clean_name(raw_tag):
    """提取标签最后一部分：'xxx;xxx;dog' -> 'Dog'"""
    if not raw_tag: return "Unknown"
    # 获取最后一段并首字母大写
    return raw_tag.split(';')[-1].strip().capitalize()

def thumbnail_to_base64(thumbnail_path: str):
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        return None
    with open(thumbnail_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def create_thumbnail(img: Image.Image, max_size=(300, 300)):
    thumb = img.copy()
    thumb.thumbnail(max_size)
    return thumb

def create_thumbnail_from_cv2(cv2_frame, size=(200, 200)):
    rgb_frame = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb_frame)
    img.thumbnail(size)
    return img

# ---------------------------
# 核心检测逻辑
# ---------------------------
def crop_animals(image_path: str, conf_threshold=0.05):
    detections = run_detector_batch.load_and_run_detector_batch(
        image_file_names=[image_path],
        model_file=str(MD_MODEL_PATH)
    )
    crops = []
    for entry in detections:
        img_path = entry["file"]
        img = Image.open(img_path).convert("RGB")
        W, H = img.size
        for det in entry["detections"]:
            if det["category"] != "1": continue
            if det["conf"] < conf_threshold: continue
            x, y, w, h = det["bbox"]
            left, top = int(x * W), int(y * H)
            right, bottom = int((x + w) * W), int((y + h) * H)
            crops.append(img.crop((left, top, right, bottom)).convert("RGB"))
    return crops

def classify_crop(crop_img: Image.Image):
    img = transform(crop_img).unsqueeze(0)
    img = img.permute(0, 2, 3, 1) # NCHW -> NHWC
    img = img.to(DEVICE)
    logits = species_model(img)
    probs = torch.softmax(logits, dim=1)[0].detach().cpu().numpy()
    best_idx = int(np.argmax(probs))
    return CLASSES[best_idx], float(probs[best_idx])

# ---------------------------
# 分析图片
# ---------------------------
def analyse_image(image_path: str, conf_threshold=0.3, generate_thumb=True):
    crops = crop_animals(image_path)
    tags = {}
    display_species = set() # 仅存名称列表
    thumbnail_path = None

    for idx, crop in enumerate(crops):
        full_tag, conf = classify_crop(crop)
        if conf >= conf_threshold:
            tags[full_tag] = tags.get(full_tag, 0) + 1
            display_species.add(get_clean_name(full_tag))
        
        if generate_thumb and thumbnail_path is None:
            thumb = create_thumbnail(crop)
            thumbnail_path = Path(tempfile.gettempdir()) / f"{Path(image_path).stem}_thumb.jpg"
            thumb.save(thumbnail_path)

    # 生成不带数量的字符串: "Dog, Cat"
    all_animals_summary = ", ".join(sorted(display_species)) if display_species else "None"
    
    return {
        "tags": tags,                      # 原始长标签数据（后端搜索用）
        "species": list(display_species),  # 简洁名称列表（前端显示用）
        "all_animals": all_animals_summary,
        "count": len(crops),
        "thumbnail": str(thumbnail_path) if thumbnail_path else None
    }

# ---------------------------
# 分析视频
# ---------------------------
def analyse_video(video_path: str, conf_threshold=0.3):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0: fps = 24
    
    max_count = 0
    best_frame_image = None
    all_video_tags = {}
    display_species = set()

    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break
        
        if frame_count % int(fps) == 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                cv2.imwrite(tmp.name, frame)
                crops = crop_animals(tmp.name)
                
                for crop in crops:
                    full_tag, conf = classify_crop(crop)
                    if conf >= conf_threshold:
                        all_video_tags[full_tag] = max(all_video_tags.get(full_tag, 0), 1)
                        display_species.add(get_clean_name(full_tag))

                if len(crops) > max_count or best_frame_image is None:
                    max_count = len(crops)
                    best_frame_image = frame.copy()
            os.remove(tmp.name)
        frame_count += 1
    cap.release()

    thumbnail_path = None
    if best_frame_image is not None:
        thumb = create_thumbnail_from_cv2(best_frame_image)
        thumbnail_path = Path(tempfile.gettempdir()) / f"{Path(video_path).stem}_v_thumb.jpg"
        thumb.save(thumbnail_path)

    all_animals_summary = ", ".join(sorted(display_species)) if display_species else "None"

    return {
        "tags": all_video_tags,
        "species": list(display_species), # 名称列表
        "all_animals": all_animals_summary,
        "count": max_count,
        "thumbnail": str(thumbnail_path) if thumbnail_path else None
    }