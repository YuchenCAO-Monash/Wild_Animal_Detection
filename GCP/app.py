from fastapi import FastAPI, UploadFile, File
from model import analyse_image, analyse_video, thumbnail_to_base64
import tempfile
import os

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/analyse-image")
async def analyse_image_api(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        input_path = tmp.name

    result = analyse_image(input_path)
    thumbnail_base64 = thumbnail_to_base64(result.get("thumbnail"))

    return {
        "filename": file.filename,
        "tags": result.get("tags", {}),           # 完整原始标签
        "species": result.get("species", []),     # 简洁名称列表 (e.g. ["Southern cassowary"])
        "all_animals": result.get("all_animals", "None"), # 汇总字符串
        "count": result.get("count", 0),
        "thumbnail_base64": thumbnail_base64,
        "thumbnail_content_type": "image/jpeg"
    }

@app.post("/analyse-video")
async def analyse_video_api(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        input_path = tmp.name

    result = analyse_video(input_path)
    thumbnail_base64 = thumbnail_to_base64(result.get("thumbnail"))

    return {
        "filename": file.filename,
        "tags": result.get("tags", {}),
        "species": result.get("species", []),     # 简洁名称列表
        "all_animals": result.get("all_animals", "None"),
        "count": result.get("count", 0),
        "thumbnail_base64": thumbnail_base64,
        "thumbnail_content_type": "image/jpeg"
    }