import json
import os
import re
import urllib.parse

import boto3
import cv2
import numpy as np

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

QUEUE_URL = os.environ.get("VISION_QUEUE_URL", "")
SHARPNESS_MIN = 20.0
CANDIDATES_PER_SEC = 4
MAX_WIDTH = 1280
RECEIPT_MAX_WIDTH = 1600
BATCH_SIZE = 8
MAX_FRAMES_PER_VIDEO = int(os.environ.get("MAX_FRAMES_PER_VIDEO", "24"))
DUP_DIFF_THRESHOLD = float(os.environ.get("DUP_DIFF_THRESHOLD", "4.5"))

VIDEO_EXTS = (".mp4", ".mov")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# Upload keys look like raw-videos/walk_<ts>_<store_slug>_<zip>.mp4 or raw-receipts/receipt_<ts>_<store_slug>_<zip>.jpg
KEY_RE = re.compile(r"^(?P<rest>.+)_(?P<slug>[a-z0-9-]+)_(?P<zip>\d{5})$")


def parse_key(key):
    """Return (walk_id, store_slug, zip_code) from an S3 key. Older keys fall back to other/00000."""
    walk_id = os.path.splitext(os.path.basename(key))[0]
    m = KEY_RE.match(walk_id)
    if m:
        return walk_id, m.group("slug"), m.group("zip")
    return walk_id, "other", "00000"


def store_display(slug):
    return slug.replace("-", " ").title() if slug else "Other"


def put_status(bucket, walk_id, stage, message, **extra):
    body = {"walk_id": walk_id, "stage": stage, "message": message}
    body.update(extra)
    s3.put_object(
        Bucket=bucket,
        Key="public/status.json",
        Body=json.dumps(body).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )


def _thumb(gray):
    return cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA).astype(np.float32)


def prepare_receipt(path):
    """Load and resize a checkout receipt photo while keeping small line-item text crisp."""
    img = cv2.imread(path)
    if img is None:
        raise ValueError("Could not decode receipt image.")
    h, w = img.shape[:2]
    if w > RECEIPT_MAX_WIDTH:
        scale = RECEIPT_MAX_WIDTH / w
        img = cv2.resize(img, (RECEIPT_MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("Could not encode receipt JPEG.")
    metrics = {
        "total_seconds": 1,
        "kept_frames": 1,
        "dropped_blurry_frames": 0,
        "skipped_duplicate_frames": 0,
        "capped_frames": 0,
    }
    return buf.tobytes(), metrics


def extract_frames(path):
    """
    Pick the sharpest frame per second, skip near-duplicate consecutive frames (standing still
    on the same shelf), and enforce MAX_FRAMES_PER_VIDEO to bound downstream AI token spend.
    Returns (frames, metrics) where frames = [(second, jpeg_bytes)].
    """
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = int(-(-total // fps)) if total else 0
    total_seconds = max(duration, 1)

    candidates = []
    dropped_blurry = 0
    skipped_duplicates = 0
    prev_thumb = None

    for sec in range(total_seconds):
        best, best_gray, best_var = None, None, -1.0
        for i in range(CANDIDATES_PER_SEC):
            pos = int((sec + i / CANDIDATES_PER_SEC) * fps)
            if total and pos >= total:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            var = cv2.Laplacian(gray, cv2.CV_64F).var()
            if var > best_var:
                best, best_gray, best_var = frame, gray, var

        if best is None or best_var <= SHARPNESS_MIN:
            dropped_blurry += 1
            continue

        cur_thumb = _thumb(best_gray)
        if prev_thumb is not None:
            diff = float(np.mean(np.abs(cur_thumb - prev_thumb)))
            if diff < DUP_DIFF_THRESHOLD:
                skipped_duplicates += 1
                continue
        prev_thumb = cur_thumb
        candidates.append((sec, best))

    cap.release()

    capped_frames = 0
    if len(candidates) > MAX_FRAMES_PER_VIDEO:
        capped_frames = len(candidates) - MAX_FRAMES_PER_VIDEO
        idxs = np.linspace(0, len(candidates) - 1, MAX_FRAMES_PER_VIDEO, dtype=int)
        candidates = [candidates[int(i)] for i in idxs]

    out = []
    for sec, img in candidates:
        h, w = img.shape[:2]
        if w > MAX_WIDTH:
            scale = MAX_WIDTH / w
            img = cv2.resize(img, (MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            out.append((sec, buf.tobytes()))

    metrics = {
        "total_seconds": total_seconds,
        "kept_frames": len(out),
        "dropped_blurry_frames": dropped_blurry,
        "skipped_duplicate_frames": skipped_duplicates,
        "capped_frames": capped_frames,
    }
    return out, metrics


def handler(event, context):
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        low = key.lower()
        is_receipt = key.startswith("raw-receipts/") and low.endswith(IMAGE_EXTS)
        is_video = key.startswith("raw-videos/") and low.endswith(VIDEO_EXTS)
        if not (is_receipt or is_video):
            continue

        walk_id, slug, zip_code = parse_key(key)
        store = store_display(slug)
        scan_type = "receipt" if is_receipt else "video"
        meta = {"store": store, "zip_code": zip_code, "scan_type": scan_type}
        local = "/tmp/" + os.path.basename(key)
        try:
            if is_receipt:
                put_status(bucket, walk_id, "slicing_frames", "Preparing receipt photo...", **meta)
                s3.download_file(bucket, key, local)
                jpeg_bytes, metrics = prepare_receipt(local)
                fkey = f"frames/{walk_id}/receipt_0000.jpg"
                s3.put_object(Bucket=bucket, Key=fkey, Body=jpeg_bytes, ContentType="image/jpeg")
                # Also store full receipt image under 60-day evidence prefix for auditability.
                s3.put_object(Bucket=bucket, Key=f"public/evidence/{walk_id}/receipt.jpg",
                              Body=jpeg_bytes, ContentType="image/jpeg", CacheControl="public, max-age=86400")
                sqs.send_message(
                    QueueUrl=QUEUE_URL,
                    MessageBody=json.dumps({
                        "bucket": bucket,
                        "walk_id": walk_id,
                        "scan_type": "receipt",
                        "store": store,
                        "zip_code": zip_code,
                        "frame_metrics": metrics,
                        "batch_index": 0,
                        "total_batches": 1,
                        "frame_keys": [fkey],
                    }),
                )
                put_status(bucket, walk_id, "analyzing_frames",
                           "Reading line items from receipt photo...", frame_metrics=metrics, **meta)
            else:
                put_status(bucket, walk_id, "slicing_frames", "Extracting sharp frames from video...", **meta)
                s3.download_file(bucket, key, local)
                frames, metrics = extract_frames(local)
                if not frames:
                    put_status(bucket, walk_id, "error", "No sharp frames found in video.",
                               frame_metrics=metrics, **meta)
                    continue
                keys = []
                for sec, data in frames:
                    fkey = f"frames/{walk_id}/sec_{sec:04d}.jpg"
                    s3.put_object(Bucket=bucket, Key=fkey, Body=data, ContentType="image/jpeg")
                    keys.append(fkey)
                batches = [keys[i:i + BATCH_SIZE] for i in range(0, len(keys), BATCH_SIZE)]
                for idx, batch in enumerate(batches):
                    sqs.send_message(
                        QueueUrl=QUEUE_URL,
                        MessageBody=json.dumps({
                            "bucket": bucket,
                            "walk_id": walk_id,
                            "scan_type": "video",
                            "store": store,
                            "zip_code": zip_code,
                            "frame_metrics": metrics,
                            "batch_index": idx,
                            "total_batches": len(batches),
                            "frame_keys": batch,
                        }),
                    )
                put_status(
                    bucket, walk_id, "analyzing_frames",
                    f"Kept {metrics['kept_frames']} unique sharp frame(s) from {metrics['total_seconds']}s "
                    f"({metrics['dropped_blurry_frames']} blurry, {metrics['skipped_duplicate_frames']} duplicate); "
                    f"sent {len(batches)} batch(es) for analysis...",
                    frame_metrics=metrics, **meta,
                )
        except Exception as e:
            print("ERROR", repr(e))
            put_status(bucket, walk_id, "error", f"Extraction failed: {e}", **meta)
            raise
        finally:
            if os.path.exists(local):
                os.remove(local)
    return {"ok": True}
