"""
attachment_analyzer.py — let Lulu read screenshots and files dropped into the chat.

Strategy: analyse the attachment ONCE up front, turn it into TEXT context, and let the
normal pipeline (planner -> tools -> safety chain) run unchanged on top of that text.

  image (png/jpg/webp)  -> one vision call on the PLANNER model (gpt-5-mini sees images);
                           tables come back as markdown, errors/numbers/names extracted
  csv / xlsx            -> pandas: shape, columns, head sample, numeric stats (local, no LLM)
  pdf                   -> PyMuPDF text of the first pages (local)
  txt / md / json / yaml-> raw text, truncated (local)

The extracted text is appended to the question as an [Attachment] block — the LLM gateway
reasons over it together with Gold data; nothing new touches the SQL safety chain.
"""

import base64
import io

MAX_CHARS = 6000          # per attachment, keep the context affordable
IMAGE_TYPES = {"png", "jpg", "jpeg", "webp", "gif"}
SHEET_TYPES = {"csv", "xlsx", "xlsm", "xls"}

VISION_PROMPT = (
    "You are the eyes of a workforce-data assistant. Extract EVERYTHING useful from this "
    "image for answering business questions: any tables as markdown, all numbers, names, "
    "dates, error messages, UI labels. Be complete but concise. Reply in the language the "
    "image content uses (中文 content -> 中文)."
)


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _analyze_image(name, data, question):
    """One vision call on the planner model; returns extracted text."""
    from llm_provider import get_provider
    provider = get_provider("planner")
    if not provider.available():
        return "(图片无法分析: planner 模型不可用)"
    ext = _ext(name)
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    b64 = base64.b64encode(data).decode()
    content = [
        {"type": "text", "text": f"{VISION_PROMPT}\n\nUser's question about this image: {question or '(none)'}"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    try:
        resp = provider.chat("You extract information from images precisely.",
                             [{"role": "user", "content": content}], max_tokens=1500)
        return (resp.text or "").strip() or "(图片分析返回空)"
    except Exception as ex:
        return f"(图片分析失败: {type(ex).__name__}: {ex})"


def _analyze_sheet(name, data):
    import pandas as pd
    try:
        if _ext(name) == "csv":
            df = pd.read_csv(io.BytesIO(data))
        else:
            df = pd.read_excel(io.BytesIO(data))
    except Exception as ex:
        return f"(表格解析失败: {type(ex).__name__}: {ex})"
    parts = [f"shape: {df.shape[0]} rows x {df.shape[1]} cols",
             "columns: " + ", ".join(f"{c} ({df[c].dtype})" for c in df.columns[:30]),
             "--- first rows ---", df.head(15).to_string(max_cols=15)]
    num = df.select_dtypes("number")
    if not num.empty:
        parts += ["--- numeric summary ---", num.describe().round(2).to_string()]
    return "\n".join(parts)[:MAX_CHARS]


def _analyze_pdf(name, data):
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc[:5])
        meta = f"({doc.page_count} pages, showing first {min(5, doc.page_count)})\n"
        return (meta + text.strip())[:MAX_CHARS] or "(PDF 没有可提取的文本 — 可能是扫描件)"
    except Exception as ex:
        return f"(PDF 解析失败: {type(ex).__name__}: {ex})"


def analyze_attachments(files, question=""):
    """files: streamlit UploadedFile list -> one combined [Attachment ...] context string."""
    blocks = []
    for f in files:
        name, data = f.name, f.getvalue()
        ext = _ext(name)
        if ext in IMAGE_TYPES:
            body = _analyze_image(name, data, question)
        elif ext in SHEET_TYPES:
            body = _analyze_sheet(name, data)
        elif ext == "pdf":
            body = _analyze_pdf(name, data)
        else:                                     # txt / md / json / yaml / log ...
            try:
                body = data.decode("utf-8", errors="replace")[:MAX_CHARS]
            except Exception:
                body = "(无法读取此文件类型)"
        blocks.append(f"[Attachment: {name} ({len(data) / 1024:.0f} KB)]\n{body}")
    return "\n\n".join(blocks)
