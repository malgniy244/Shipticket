"""
Targeted re-detection of page 7 with higher DPI and stronger prompt.
"""
import base64
import json
import sys
import fitz
from openai import OpenAI

PDF = "/home/ubuntu/upload/testingfile.pdf"
PAGE_NUM = 7  # 1-indexed
DPI = 250  # Higher DPI for clearer handwriting

SYSTEM_PROMPT = """You are an expert at reading handwritten ship ticket numbers on scanned auction paperwork.

The handwriting on this page is messy but always follows the pattern "ST: XXXXXX" or "ST XXXXXX" where XXXXXX is a 6-digit number.

IMPORTANT: The number is ALWAYS exactly 6 digits. If you see something that looks like fewer digits, look again — a digit is likely faint or merged with another character.

Common digit confusions in messy handwriting:
- "2" can look like "7" or "3"
- "5" can look like "6" or "3"
- "3" can look like "8" or "2"

Look at the top-right area of the image for handwritten text starting with "ST:".

Return JSON: {"candidates": [{"value": "XXXXXX", "source": "handwritten", "confidence": 0.0-1.0, "crossed_out": false, "corrected_from": ""}]}
If no number is found, return {"candidates": []}."""

client = OpenAI()

doc = fitz.open(PDF)
page = doc[PAGE_NUM - 1]
mat = fitz.Matrix(DPI / 72, DPI / 72)
pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
jpeg_bytes = pix.tobytes("jpeg")
doc.close()

b64 = base64.b64encode(jpeg_bytes).decode("ascii")
data_url = f"data:image/jpeg;base64,{b64}"

PAGE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "source": {"type": "string", "enum": ["printed", "sticker", "handwritten"]},
                    "confidence": {"type": "number"},
                    "crossed_out": {"type": "boolean"},
                    "corrected_from": {"type": "string"},
                },
                "required": ["value", "source", "confidence", "crossed_out", "corrected_from"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

for attempt in range(3):
    resp = client.chat.completions.create(
        model="gpt-5",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is the 6-digit ship ticket number written in the top-right of this page? Look carefully at the handwriting after 'ST:'."},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "page_detection",
                "strict": True,
                "schema": PAGE_RESULT_SCHEMA,
            },
        },
        max_completion_tokens=512,
    )
    result = json.loads(resp.choices[0].message.content)
    print(f"Attempt {attempt+1}: {result}")
