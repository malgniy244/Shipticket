"""
Test models on the actual sticker pages that failed in job 65ab7b15.
We need to use the file from that job. Based on the detection pattern,
the file is likely SKM_C250i26070917180.pdf but with different pages.
We'll test all pages of both available PDFs to find sticker pages.
"""
import sys, os, time, json, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from openai import OpenAI

client = OpenAI()

SYSTEM_PROMPT = """You are a document analysis assistant specializing in ship ticket identification.

Your task: examine the provided page image and identify any ship ticket number present.

Ship ticket numbers are 6-digit numbers (e.g. 248256, 300588). They appear:
- On a WHITE RECTANGULAR LABEL STICKER (usually in a corner)
- Printed directly on the document header  
- Handwritten on the page

IMPORTANT: Focus especially on WHITE RECTANGULAR LABEL STICKERS. These are small adhesive labels
with a 6-digit number printed on them. They may be in any corner of the page.

IGNORE: banknote serial numbers, PMG/NGC grade numbers, customer IDs, lot numbers.

Also detect: is there a bright PINK or MAGENTA rectangular sticker anywhere on the page?

Return JSON:
{
  "candidates": [{"ticket": "248256", "confidence": 0.95, "source": "sticker", "second_pass": false}],
  "pink_marker": false
}
source: "printed", "sticker", or "handwritten"
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


def call_model(model: str, jpeg_bytes: bytes) -> tuple[dict | None, float, str | None]:
    b64 = base64.b64encode(jpeg_bytes).decode()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Identify the ship ticket number on this page."},
            ],
        },
    ]

    kwargs = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "page_detection", "strict": True, "schema": PAGE_RESULT_SCHEMA},
        },
    }
    if model.startswith("gpt-"):
        kwargs["max_completion_tokens"] = 1024
    elif model.startswith("gemini-"):
        kwargs["max_tokens"] = 4096

    t0 = time.time()
    try:
        resp = client.chat.completions.create(**kwargs)
        latency = time.time() - t0
        raw = resp.choices[0].message.content
        if not raw:
            return None, latency, "empty_response"
        return json.loads(raw), latency, None
    except Exception as e:
        return None, latency, str(e)[:200]


def scan_all_pages(pdf_path: str, models: list[str]):
    doc = fitz.open(pdf_path)
    n = len(doc)
    doc.close()
    print(f"\nScanning all {n} pages of {os.path.basename(pdf_path)}")
    print(f"{'Model':<28} {'Pg':>3} {'Lat':>6} {'Result':<55} {'Err'}")
    print("-" * 100)

    for idx in range(n):
        img = render_page(pdf_path, idx, dpi=150)
        for model in models:
            result, lat, err = call_model(model, img)
            if err:
                print(f"{model:<28} {idx+1:>3} {lat:>5.1f}s  ERROR: {err[:50]}")
            else:
                cands = result.get("candidates", [])
                pink = result.get("pink_marker", False)
                if cands:
                    c = cands[0]
                    s = f"{c['ticket']} ({c['source']}, conf={c['confidence']:.2f})"
                else:
                    s = "no detection"
                if pink:
                    s += " [PINK]"
                print(f"{model:<28} {idx+1:>3} {lat:>5.1f}s  {s}")


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/upload/SKM_C250i26070917180.pdf"
    models = ["gpt-5", "gemini-3-flash-preview"]
    scan_all_pages(pdf, models)
