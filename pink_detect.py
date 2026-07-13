"""
pink_detect.py — Local pink sticker detection via classical image processing.

Detects bright pink/magenta rectangular stickers on PDF page images using
OpenCV HSV color thresholding. No API call required — runs in milliseconds.

Used by fast mode to set block boundaries without consuming vision API quota.
The API-based pink_marker field in detect.py remains as a fallback/cross-check.

Decision 13: fast mode uses local pink detection as the primary boundary signal
for non-TIB batches, with the vision API called only on each block's first page.

Public API:
  detect_pink_sticker(jpeg_bytes) -> bool
  detect_pink_sticker_debug(jpeg_bytes) -> dict   # per-page score data
  detect_pink_stickers_batch(jpeg_bytes_list) -> list[bool]
  detect_pink_stickers_batch_debug(jpeg_bytes_list) -> list[dict]
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

# Minimum fill ratio: pink pixels / bounding-box area
_MIN_FILL_RATIO = 0.30


def _run_detection(jpeg_bytes: bytes) -> dict:
    """
    Core detection logic. Returns a dict with all intermediate values:
      detected: bool
      largest_blob_area_px: int | None
      largest_blob_fill_ratio: float | None
      largest_blob_bbox: [x, y, w, h] | None
      hue_range_matched: "range1" | "range2" | "both" | None
      total_pink_pixels: int
      rejection_reason: str | None  (why the best blob was rejected, if any)
      cv2_available: bool
    """
    result: dict = {
        "detected": False,
        "largest_blob_area_px": None,
        "largest_blob_fill_ratio": None,
        "largest_blob_bbox": None,
        "hue_range_matched": None,
        "total_pink_pixels": 0,
        "rejection_reason": None,
        "cv2_available": _CV2_AVAILABLE,
    }

    if not _CV2_AVAILABLE:
        result["rejection_reason"] = "cv2_not_available"
        return result

    # Decode JPEG
    nparr = np.frombuffer(jpeg_bytes, np.uint8)
    img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        result["rejection_reason"] = "jpeg_decode_failed"
        return result

    # Convert to HSV
    img_hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

    # Create pink mask (two hue ranges for wrap-around)
    mask1 = cv2.inRange(img_hsv, _PINK_HSV_LOWER1, _PINK_HSV_UPPER1)
    mask2 = cv2.inRange(img_hsv, _PINK_HSV_LOWER2, _PINK_HSV_UPPER2)
    mask = cv2.bitwise_or(mask1, mask2)

    # Track which hue range contributed
    px1 = int(np.count_nonzero(mask1))
    px2 = int(np.count_nonzero(mask2))
    if px1 > 0 and px2 > 0:
        result["hue_range_matched"] = "both"
    elif px1 > 0:
        result["hue_range_matched"] = "range1"
    elif px2 > 0:
        result["hue_range_matched"] = "range2"

    result["total_pink_pixels"] = int(np.count_nonzero(mask))

    # Morphological cleanup: close small gaps, remove noise
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Find contours
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        result["rejection_reason"] = "no_contours"
        return result

    # Find the largest contour
    best_contour = max(contours, key=cv2.contourArea)
    best_area = cv2.contourArea(best_contour)
    result["largest_blob_area_px"] = int(best_area)

    if best_area < _MIN_STICKER_AREA_PX:
        result["rejection_reason"] = f"area_too_small ({int(best_area)} < {_MIN_STICKER_AREA_PX})"
        return result

    x, y, w, h = cv2.boundingRect(best_contour)
    result["largest_blob_bbox"] = [int(x), int(y), int(w), int(h)]

    if w == 0 or h == 0:
        result["rejection_reason"] = "zero_dimension_bbox"
        return result

    aspect = min(w, h) / max(w, h)
    if aspect < _MIN_ASPECT_RATIO:
        result["rejection_reason"] = f"aspect_too_narrow ({aspect:.3f} < {_MIN_ASPECT_RATIO})"
        return result

    roi_mask = mask[y:y+h, x:x+w]
    fill_ratio = float(np.count_nonzero(roi_mask) / (w * h))
    result["largest_blob_fill_ratio"] = round(fill_ratio, 4)

    if fill_ratio < _MIN_FILL_RATIO:
        result["rejection_reason"] = f"fill_ratio_too_low ({fill_ratio:.3f} < {_MIN_FILL_RATIO})"
        return result

    result["detected"] = True
    return result


def detect_pink_sticker(jpeg_bytes: bytes) -> bool:
    """
    Return True if a bright pink/magenta rectangular sticker is detected in the image.

    Args:
        jpeg_bytes: JPEG image bytes (from render_page_to_jpeg)

    Returns:
        True if a pink sticker is found, False otherwise.
        Falls back to False if OpenCV is not available.
    """
    return _run_detection(jpeg_bytes)["detected"]


def detect_pink_sticker_debug(jpeg_bytes: bytes) -> dict:
    """
    Return full per-page detection diagnostics.

    Returns a dict with:
      detected: bool
      largest_blob_area_px: int | None   — area of the largest pink blob in pixels
      largest_blob_fill_ratio: float | None  — fraction of bounding box that is pink
      largest_blob_bbox: [x, y, w, h] | None  — bounding box of the largest blob
      hue_range_matched: "range1" | "range2" | "both" | None
        range1 = H 140–180 (magenta/pink); range2 = H 0–15 (red-pink wrap-around)
      total_pink_pixels: int  — raw pink pixel count before morphology
      rejection_reason: str | None  — why detection failed (if detected=False)
      cv2_available: bool
    """
    return _run_detection(jpeg_bytes)


def detect_pink_stickers_batch(jpeg_bytes_list: list[bytes]) -> list[bool]:
    """
    Detect pink stickers on multiple pages.

    Args:
        jpeg_bytes_list: list of JPEG image bytes, one per page (1-indexed order)

    Returns:
        list of bool, one per page
    """
    return [detect_pink_sticker(b) for b in jpeg_bytes_list]


def detect_pink_stickers_batch_debug(jpeg_bytes_list: list[bytes]) -> list[dict]:
    """
    Return full per-page detection diagnostics for multiple pages.

    Args:
        jpeg_bytes_list: list of JPEG image bytes, one per page (1-indexed order)

    Returns:
        list of debug dicts (one per page), in the same order as input
    """
    return [detect_pink_sticker_debug(b) for b in jpeg_bytes_list]
