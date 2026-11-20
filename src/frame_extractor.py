import json
import os
import re
import urllib.parse

import boto3
import cv2

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

QUEUE_URL = os.environ.get("VISION_QUEUE_URL", "")
SHARPNESS_MIN = 20.0
CANDIDATES_PER_SEC = 4
MAX_WIDTH = 1280
BATCH_SIZE = 8

# Upload keys look like raw-videos/walk_<timestamp>_<store_slug>_<zip>.mp4
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


def extract_frames(path):
    """Return (frames, metrics). frames = [(second, jpeg_bytes)]."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = int(-(-total // fps)) if total else 0
    total_seconds = max(duration, 1)
    out = []
    for sec in range(total_seconds):
        best, best_var = None, -1.0
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
                best, best_var = frame, var
        if best is not None and best_var > SHARPNESS_MIN:
            h, w = best.shape[:2]
            if w > MAX_WIDTH:
                scale = MAX_WIDTH / w
                best = cv2.resize(best, (MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", best, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                out.append((sec, buf.tobytes()))
    cap.release()
    metrics = {
        "total_seconds": total_seconds,
        "kept_frames": len(out),
        "dropped_blurry_frames": total_seconds - len(out),
    }
    return out, metrics


def handler(event, context):
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        if not key.startswith("raw-videos/") or not key.lower().endswith((".mp4", ".mov")):
            continue
        walk_id, slug, zip_code = parse_key(key)
        store = store_display(slug)
        meta = {"store": store, "zip_code": zip_code}
        local = "/tmp/" + os.path.basename(key)
        try:
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
                        "store": store,
                        "zip_code": zip_code,
                        "frame_metrics": metrics,
                        "batch_index": idx,
                        "total_batches": len(batches),
                        "frame_keys": batch,
                    }),
                )
            put_status(bucket, walk_id, "analyzing_frames",
                       f"Kept {metrics['kept_frames']} of {metrics['total_seconds']} seconds "
                       f"({metrics['dropped_blurry_frames']} too blurry); sent {len(batches)} batch(es) for analysis...",
                       frame_metrics=metrics, **meta)
        except Exception as e:
            print("ERROR", repr(e))
            put_status(bucket, walk_id, "error", f"Frame extraction failed: {e}", **meta)
            raise
        finally:
            if os.path.exists(local):
                os.remove(local)
    return {"ok": True}
