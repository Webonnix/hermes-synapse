"""Extracts readable text out of an uploaded document so the agent can act on
it, rather than just being told a file arrived.

Plain-text-ish formats (.txt/.md/.csv/.json/...) are decoded directly; PDF
(pypdf), DOCX (python-docx) and spreadsheets (pandas/openpyxl, already a
project dependency) go through a real parser. Anything else — images, zips,
audio-as-document, proprietary binary formats — has no extractor here and
`extract_text` returns None, so the caller can tell the model the file
arrived but couldn't be read automatically instead of silently ignoring it.

Shared by backend/bot.py (the main Vexa Telegram bot) and backend/agent_bot.py
(per-agent bots), so a document behaves the same way regardless of which bot
receives it.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

logger = logging.getLogger("hermes.document_extract")

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".html", ".htm", ".css", ".sh",
}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
SPREADSHEET_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram bot API's own file-download cap
MAX_EXTRACT_CHARS = 12000


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def extract_text(data: bytes, filename: str, mime_type: str = "") -> Optional[str]:
    """Returns readable text pulled out of `data`, or None if the format has
    no extractor here. Callers are responsible for truncating to
    MAX_EXTRACT_CHARS and noting truncation if they care (see
    build_document_prompt below, which already does)."""
    ext = os.path.splitext(filename or "")[1].lower()
    mime_type = mime_type or ""

    if ext == ".pdf" or mime_type == "application/pdf":
        return _extract_pdf(data)
    if ext == ".docx" or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx(data)
    if ext in SPREADSHEET_EXTENSIONS or mime_type in SPREADSHEET_MIME_TYPES:
        return _extract_spreadsheet(data)
    if ext in TEXT_EXTENSIONS or mime_type.startswith("text/") or mime_type == "application/json":
        return _decode_text(data)
    return None


def build_document_prompt(filename: str, mime_type: str, size_bytes: int, data: bytes, caption: str = "") -> str:
    """Builds the prompt to hand the agent for one received document: the
    extracted (and truncated) content when the format is readable, or an
    honest note that it isn't, either way including the user's caption if
    they gave one."""
    caption = (caption or "").strip()
    text_content = extract_text(data, filename, mime_type)
    if text_content:
        truncated = text_content[:MAX_EXTRACT_CHARS]
        note = (
            "\n\n[Файл обрезан — показана только начальная часть.]"
            if len(text_content) > MAX_EXTRACT_CHARS else ""
        )
        intro = caption or f"Пользователь прислал файл «{filename}». Проанализируй его содержимое и ответь по существу."
        return f"{intro}\n\n[Содержимое файла «{filename}»]:\n{truncated}{note}"

    return caption or (
        f"Пользователь прислал файл «{filename}» ({mime_type or 'неизвестный тип'}, "
        f"{human_size(size_bytes)}). Автоматически прочитать содержимое в этом формате не удалось — "
        f"уточни у пользователя, что нужно сделать с этим файлом."
    )


def _decode_text(data: bytes) -> Optional[str]:
    for encoding in ("utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _extract_pdf(data: bytes) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — cannot extract PDF text")
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(p for p in pages if p.strip())
        return text or None
    except Exception:
        logger.exception("Failed to extract text from PDF")
        return None


def _extract_docx(data: bytes) -> Optional[str]:
    try:
        import docx
    except ImportError:
        logger.warning("python-docx not installed — cannot extract DOCX text")
        return None
    try:
        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts) or None
    except Exception:
        logger.exception("Failed to extract text from DOCX")
        return None


def _extract_spreadsheet(data: bytes) -> Optional[str]:
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas not installed — cannot extract spreadsheet text")
        return None
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        parts = [f"[Лист «{name}»]\n{df.to_csv(index=False)}" for name, df in sheets.items()]
        return "\n\n".join(parts) or None
    except Exception:
        logger.exception("Failed to extract text from spreadsheet")
        return None
