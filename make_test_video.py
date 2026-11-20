import cv2
import numpy as np

W, H, FPS, SECONDS = 1280, 720, 30, 3
TAGS = [
    ("Friendly Farms Whole Milk", "1 gal", "$3.19"),
    ("Goldhen Large Eggs", "12 ct", "$2.29"),
    ("Clancy's Wavy Potato Chips", "10 oz", "$1.89"),
]

rng = np.random.default_rng(7)
bg = np.full((H, W, 3), (70, 80, 90), np.uint8)
for y in range(0, H, 24):  # shelf slats texture
    cv2.line(bg, (0, y), (W, y), (50, 58, 66), 2)
noise = rng.integers(-12, 12, (H, W, 3), dtype=np.int16)
bg = np.clip(bg.astype(np.int16) + noise, 0, 255).astype(np.uint8)

for i, (name, size, price) in enumerate(TAGS):
    x0, y0 = 60, 40 + i * 225
    cv2.rectangle(bg, (x0, y0), (W - 60, y0 + 190), (255, 255, 255), -1)
    cv2.rectangle(bg, (x0, y0), (W - 60, y0 + 190), (0, 0, 0), 4)
    cv2.rectangle(bg, (x0, y0), (x0 + 30, y0 + 190), (0, 0, 200), -1)
    cv2.putText(bg, name, (x0 + 60, y0 + 70), cv2.FONT_HERSHEY_DUPLEX, 1.7, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(bg, size, (x0 + 60, y0 + 150), cv2.FONT_HERSHEY_DUPLEX, 1.5, (60, 60, 60), 3, cv2.LINE_AA)
    cv2.putText(bg, price, (W - 420, y0 + 150), cv2.FONT_HERSHEY_DUPLEX, 3.2, (0, 0, 180), 6, cv2.LINE_AA)

out = cv2.VideoWriter("/work/test_aisle.mp4", cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
for f in range(FPS * SECONDS):
    frame = bg.copy()
    shift = int(3 * np.sin(f / 10.0))  # tiny handheld jitter
    M = np.float32([[1, 0, shift], [0, 1, 0]])
    frame = cv2.warpAffine(frame, M, (W, H), borderMode=cv2.BORDER_REPLICATE)
    out.write(frame)
out.release()
print("wrote /work/test_aisle.mp4")
