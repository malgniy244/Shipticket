"""
pink_detect.py — Local pink sticker detection via classical image processing.

Detects bright pink/magenta rectangular stickers on PDF page images using
OpenCV HSV color thresholding. No API call required — runs in milliseconds.

Used by fast mode to set block boundaries without consuming vision API quota.
The API-based pink_marker field in detect.py remains as a fallback/cross-check.

Decision 13: fast mode uses local pink detection as the primary boundary signal
for non-TIB batches, with the vision API called only on each block's first page.
"""

from __future__ import annotations

import numpy as np

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


# HSV range for pink stickers as observed in fixture scans.
# Actual sticker color: pastel/light pink, H≈159 (OpenCV 0–180 scale), S≈77, V≈254.
# OpenCV H=159 corresponds to ~318° in standard HSV (magenta-pink).
# We use a wide hue range (140–180) and low saturation floor (50) to catch
# both vivid hot-pink and pastel pink stickers.
# Also catch hue 0–15 for red-pink wrap-around.
_PINK_HSV_LOWER1 = np.array([140, 50, 100], dtype=np.uint8)  # magenta/pink upper hue range
_PINK_HSV_UPPER1 = np.array([180, 255, 255], dtype=np.uint8)
_PINK_HSV_LOWER2 = np.array([0, 80, 100], dtype=np.uint8)    # red-pink wrap-around (0–15°)
_PINK_HSV_UPPER2 = np.array([15, 255, 255], dtype=np.uint8)

# Minimum area of the pink blob to count as a sticker (pixels at 150 DPI)
# A 1cm × 1cm sticker at 150 DPI ≈ 59×59 = 3481 px²; use 2000 as minimum
_MIN_STICKER_AREA_PX = 2000

# Minimum aspect ratio (shorter/longer side) — stickers are roughly rectangular
_MIN_ASPECT_RATIO = 0.15  # very elongated is still OK


def detect_pink_sticker(jpeg_bytes: bytes) -> bool:
    """
    Return True if a bright pink/magenta rectangular sticker is detected in the image.

    Args:
        jpeg_bytes: JPEG image bytes (from render_page_to_jpeg)

    Returns:
        True if a pink sticker is found, False otherwise.
        Falls back to False if OpenCV is not available.
    """
    if not _CV2_AVAILABLE:
        return False

    # Decode JPEG
    nparr = np.frombuffer(jpeg_bytes, np.uint8)
    img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        return False

    # Convert to HSV
    img_hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

    # Create pink mask (two hue ranges for wrap-around)
    mask1 = cv2.inRange(img_hsv, _PINK_HSV_LOWER1, _PINK_HSV_UPPER1)
    mask2 = cv2.inRange(img_hsv, _PINK_HSV_LOWER2, _PINK_HSV_UPPER2)
    mask = cv2.bitwise_or(mask1, mask2)

    # Morphological cleanup: close small gaps, remove noise
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Find contours
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < _MIN_STICKER_AREA_PX:
            continue

        # Check that the blob is roughly rectangular
        x, y, w, h = cv2.boundingRect(contour)
        if w == 0 or h == 0:
            continue
        aspect = min(w, h) / max(w, h)
        if aspect < _MIN_ASPECT_RATIO:
            continue

        # Check fill ratio: the pink pixels should fill most of the bounding box
        roi_mask = mask[y:y+h, x:x+w]
        fill_ratio = np.count_nonzero(roi_mask) / (w * h)
        if fill_ratio < 0.3:
            continue

        return True

    return False


def detect_pink_stickers_batch(jpeg_bytes_list: list[bytes]) -> list[bool]:
    """
    Detect pink stickers on multiple pages.

    Args:
        jpeg_bytes_list: list of JPEG image bytes, one per page (1-indexed order)

    Returns:
        list of bool, one per page
    """
    return [detect_pink_sticker(b) for b in jpeg_bytes_list]
