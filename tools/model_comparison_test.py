"""
Model comparison test: gpt-5 vs gemini-3-flash-preview for ship ticket detection.
Tests on pages from the uploaded PDFs to compare latency, error rate, and accuracy.
"""
import sys, os, time, json, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # PyMuPDF
from openai import OpenAI

client = OpenAI()

# The system prompt used for sticker detection
STICKER_SYSTEM_PROMPT = """You are a document analysis assistant specializing in ship ticket identification.

Your task: examine the provided page image and identify any ship ticket number present.

Ship ticket numbers are 6-digit numbers (e.g. 248256, 300588). They appear:
- On a WHITE RECTANGULAR LABEL STICKER (usually in a corner — top-right, bottom-right, or top-left)
- Printed directly on the document header
- Handwritten on the page

IGNORE: banknote serial numbers, PMG/NGC grade numbers, customer IDs, lot numbers, auction numbers.

Also detect: is there a bright PINK or MAGENTA rectangular sticker anywhere on the page?

Return JSON matching this schema exactly:
{
  "candidates": [
    {
      "ticket": "248256",
      "confidence": 0.95,
      "source": "sticker",
      "second_pass": false
    }
  ],
  "pink_marker": false
}

source must be one of: "printed", "sticker", "handwritten"
If no ship ticket number is visible, return {"candidates": [], "pink_marker": false}
"""

PAGE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticket": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source": {"type": "string", "enum": ["printed", "sticker", "handwritten"]},
                    "second_pass": {"type": "boolean"},
                },
                "required": ["ticket", "confidence", "source", "second_pass"],
                "additionalProperties": False,
            },
        },
        "pink_marker": {"type": "boolean"},
    },
    "required": ["candidates", "pink_marker"],
    "additionalProperties": False,
}


def render_page(pdf_path: str, page_idx: int, dpi: int = 150) -> bytes:
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    jpeg_bytes = pix.tobytes("jpeg", jpg_quality=85)
    doc.close()
    return jpeg_bytes


def call_model(model: str, jpeg_bytes: bytes, use_thinking: bool = False) -> tuple[dict | None, float, str | None]:
    """Returns (result_dict, latency_seconds, error_string)"""
    b64 = base64.b64encode(jpeg_bytes).decode()
    messages = [
        {"role": "system", "content": STICKER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {"role": "user", "content": "Identify the ship ticket number on this page."},
            ],
        },
    ]
    # Fix message structure
    messages = [
        {"role": "system", "content": STICKER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": "Identify the ship ticket number on this page.",
                },
            ],
        },
    ]

    kwargs = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "page_detection",
                "strict": True,
                "schema": PAGE_RESULT_SCHEMA,
            },
        },
    }

    # Model-specific token settings
    if model.startswith("gpt-"):
        kwargs["max_completion_tokens"] = 1024
    elif model.startswith("gemini-"):
        kwargs["max_tokens"] = 4096  # Gemini needs max_tokens, not max_completion_tokens
    else:
        kwargs["max_tokens"] = 2048

    # Disable thinking for speed comparison
    if not use_thinking:
        if model.startswith("gpt-"):
            kwargs["extra_body"] = {"reasoning": {"effort": "low"}}
        elif model.startswith("claude-"):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    t0 = time.time()
    try:
        resp = client.chat.completions.create(**kwargs)
        latency = time.time() - t0
        raw = resp.choices[0].message.content
        if not raw:
            return None, latency, "empty_response"
        result = json.loads(raw)
        return result, latency, None
    except Exception as e:
        latency = time.time() - t0
        return None, latency, str(e)[:200]


def test_models_on_pdf(pdf_path: str, page_indices: list[int], models: list[str]):
    """Test multiple models on specific pages of a PDF."""
    print(f"\nPDF: {pdf_path}")
    print(f"Pages (0-indexed): {page_indices}")
    print(f"Models: {models}")
    print()

    # Render pages once
    print("Rendering pages...")
    page_images = {}
    for idx in page_indices:
        page_images[idx] = render_page(pdf_path, idx, dpi=150)
        print(f"  Page {idx+1}: {len(page_images[idx])} bytes JPEG at 150 DPI")

    print()
    print(f"{'Model':<30} {'Page':<6} {'Latency':>8} {'Result':<50} {'Error'}")
    print("-" * 110)

    results = {}
    for model in models:
        results[model] = {"latencies": [], "errors": 0, "detections": 0}
        for idx in page_indices:
            result, latency, error = call_model(model, page_images[idx])
            results[model]["latencies"].append(latency)
            if error:
                results[model]["errors"] += 1
                print(f"{model:<30} {idx+1:<6} {latency:>7.1f}s  ERROR: {error[:50]}")
            else:
                candidates = result.get("candidates", [])
                pink = result.get("pink_marker", False)
                if candidates:
                    results[model]["detections"] += 1
                    c = candidates[0]
                    det_str = f"{c['ticket']} ({c['source']}, conf={c['confidence']:.2f})"
                    if pink:
                        det_str += " [PINK]"
                else:
                    det_str = "no detection" + (" [PINK]" if pink else "")
                print(f"{model:<30} {idx+1:<6} {latency:>7.1f}s  {det_str}")

    print()
    print("=== SUMMARY ===")
    for model in models:
        r = results[model]
        avg_lat = sum(r["latencies"]) / len(r["latencies"]) if r["latencies"] else 0
        print(f"{model:<30} avg={avg_lat:.1f}s  errors={r['errors']}/{len(page_indices)}  detections={r['detections']}/{len(page_indices)}")


if __name__ == "__main__":
    # Test on fixture #5 (17-page TIB batch) — pages 1, 5, 9, 12, 15 (first pages of each block)
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/upload/SKM_C250i26070916020.pdf"

    # Test pages: 0-indexed
    # For fixture #5: blocks start at pages 1,5,9,12,15 (1-indexed) = 0,4,8,11,14 (0-indexed)
    test_pages = [0, 4, 8, 11, 14]

    models_to_test = [
        "gpt-5",
        "gemini-3-flash-preview",
        "gpt-5-mini",
    ]

    test_models_on_pdf(pdf_path, test_pages, models_to_test)
