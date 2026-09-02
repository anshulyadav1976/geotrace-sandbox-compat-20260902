#!/usr/bin/env python3
"""Run the real GeoTrace image and GeoCLIP CPU compatibility probe."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

DEFAULT_IMAGE_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0a/"
    "Golden_Gate_Bridge_0002.jpg/1280px-Golden_Gate_Bridge_0002.jpg"
)


def now_ms() -> int:
    return round(time.time() * 1000)


def emit(event_type: str, stage: str, message: str, **extra: object) -> None:
    print(json.dumps({"type": event_type, "stage": stage, "message": message,
                      "tsMs": now_ms(), **extra}, default=str), flush=True)


def timed(fn):
    start = time.monotonic()
    value = fn()
    return value, round((time.monotonic() - start) * 1000)


def command_output(args: list[str]) -> tuple[int | None, str]:
    try:
        run = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20, check=False)
        return run.returncode, run.stdout[-4000:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def diagnostics() -> dict:
    disk = shutil.disk_usage("/")
    cpu_count = os.cpu_count()
    node_code, node_version = command_output(["node", "--version"])
    try:
        import resource
        peak_rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except Exception:
        peak_rss_bytes = None
    return {
        "platform": platform.platform(), "architecture": platform.machine(),
        "python": sys.version, "node": node_version.strip() if node_code == 0 else None,
        "cpuCount": cpu_count, "disk": {"total": disk.total, "free": disk.free},
        "workspace": str(Path.cwd()), "peakRssBytes": peak_rss_bytes,
        "isRoot": os.geteuid() == 0 if hasattr(os, "geteuid") else None,
        "packageManagers": {name: shutil.which(name) is not None
                            for name in ("apt-get", "apk", "dnf", "yum")},
    }


def install_requirements(requirements: Path) -> dict:
    before = shutil.disk_usage("/").free
    emit("lab.status", "dependencies", "Installing GeoCLIP dependencies")
    start = time.monotonic()
    run = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements)],
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         check=False)
    return {"success": run.returncode == 0, "exitCode": run.returncode,
            "durationMs": round((time.monotonic() - start) * 1000),
            "diskConsumedBytes": before - shutil.disk_usage("/").free,
            "outputTail": run.stdout[-5000:]}


def process_image(image_path: Path, output_dir: Path) -> dict:
    from PIL import Image, ImageFilter
    import cv2
    import exifread

    emit("lab.status", "image", "Opening and processing source image")
    with Image.open(image_path) as image:
        dimensions, mode = image.size, image.mode
        with image_path.open("rb") as raw:
            exif = {str(k): str(v) for k, v in exifread.process_file(raw, details=False).items()}
    cv_image = cv2.imread(str(image_path))
    if cv_image is None:
        raise RuntimeError("OpenCV could not read downloaded source image")
    height, width = cv_image.shape[:2]
    resized = cv2.resize(cv_image, (min(1024, width), max(1, round(height * min(1024, width) / width))))
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(
        cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY))
    cv2.imwrite(str(output_dir / "enhanced-gray.jpg"), enhanced)
    pil = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
    pil.filter(ImageFilter.SHARPEN).save(output_dir / "resized-sharpened.jpg")
    h, w = resized.shape[:2]
    crops = {
        "full": resized, "center": resized[h//4:3*h//4, w//4:3*w//4],
        "upper": resized[:h//2, :], "lower": resized[h//2:, :],
        "left": resized[:, :w//2], "right": resized[:, w//2:],
    }
    outputs = []
    for name, crop in crops.items():
        path = output_dir / f"{name}.jpg"
        if not cv2.imwrite(str(path), crop):
            raise RuntimeError(f"Could not write {path.name}")
        reread = cv2.imread(str(path))
        if reread is None:
            raise RuntimeError(f"Could not reread {path.name}")
        outputs.append({"name": path.name, "bytes": path.stat().st_size,
                        "shape": list(reread.shape)})
    emit("lab.finding", "image", "Six image regions generated", count=len(crops))
    return {"dimensions": list(dimensions), "mode": mode, "exif": exif,
            "outputs": outputs, "enhanced": "enhanced-gray.jpg"}


def ocr_test(output_dir: Path) -> dict:
    try:
        import pytesseract
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        return {"available": False, "reason": str(exc), "texts": []}
    texts = []
    for name in ("full.jpg", "enhanced-gray.jpg", "upper.jpg", "center.jpg"):
        path = output_dir / name
        if not path.exists():
            continue
        try:
            text = pytesseract.image_to_string(str(path)).strip()
            texts.append({"crop": name, "text": text, "confidence": None})
        except Exception as exc:
            texts.append({"crop": name, "text": "", "confidence": None, "error": str(exc)})
    return {"available": True, "version": version, "texts": texts}


def json_number(value):
    return value.item() if hasattr(value, "item") else float(value)


def geoclip_test(image_path: Path) -> dict:
    emit("lab.status", "model", "Loading GeoCLIP CPU model")
    try:
        import torch
        from geoclip import GeoCLIP
    except Exception as exc:
        return {"success": False, "category": "dependency conflict", "error": repr(exc)}
    try:
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        model, init_ms = timed(lambda: GeoCLIP())
        model.eval()
        def predict():
            gps, scores = model.predict(str(image_path), top_k=5)
            return gps, scores
        (gps, scores), first_ms = timed(predict)
        (_, _), warm_ms = timed(predict)
        predictions = []
        for rank, (point, score) in enumerate(zip(gps, scores), 1):
            predictions.append({"rank": rank, "lat": json_number(point[0]),
                                "lon": json_number(point[1]), "score": json_number(score)})
        emit("lab.finding", "model", "Top candidate received", **predictions[0])
        return {"success": True, "torch": torch.__version__, "initMs": init_ms,
                "firstInferenceMs": first_ms, "warmInferenceMs": warm_ms,
                "predictions": predictions}
    except MemoryError as exc:
        return {"success": False, "category": "memory exhaustion", "error": repr(exc)}
    except Exception as exc:
        text = traceback.format_exc()
        category = "model asset problem" if any(x in text.lower() for x in ("download", "weight", "asset")) else "unknown runtime issue"
        return {"success": False, "category": category, "error": text[-6000:]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--image-url", default=DEFAULT_IMAGE_URL)
    parser.add_argument("--result", default="results/runtime-result.json")
    args = parser.parse_args()
    root = Path.cwd()
    output_dir = root / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = Path(args.result)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"execution": "started", "startedAtMs": now_ms(), "diagnostics": diagnostics(),
              "sample": {"url": args.image_url, "expectedRegion": "San Francisco, California"}}
    emit("lab.status", "boot", "Sandbox ready")
    if args.install:
        report["dependencies"] = install_requirements(Path(__file__).with_name("requirements.txt"))
    image_path = root / "sample.jpg"
    try:
        emit("lab.status", "network", "Downloading public outdoor sample image")
        _, download_ms = timed(lambda: urllib.request.urlretrieve(args.image_url, image_path))
        report["download"] = {"success": True, "durationMs": download_ms, "bytes": image_path.stat().st_size}
        report["image"] = process_image(image_path, output_dir)
        report["ocr"] = ocr_test(output_dir)
        report["geoclip"] = geoclip_test(image_path)
        report["execution"] = "completed"
    except Exception:
        report["execution"] = "failed"
        report["failure"] = traceback.format_exc()[-8000:]
    report["finishedAtMs"] = now_ms()
    report["durationMs"] = report["finishedAtMs"] - report["startedAtMs"]
    report["finalDiagnostics"] = diagnostics()
    result_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    emit("lab.completed" if report["execution"] == "completed" else "lab.failed", "complete",
         "GeoTrace probe completed", durationMs=report["durationMs"], success=report["execution"] == "completed")
    return 0 if report["execution"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
