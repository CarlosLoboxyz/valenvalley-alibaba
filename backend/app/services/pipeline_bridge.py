"""Clean import surface for the ``scripts/`` pipeline modules.

Importing this module puts ``scripts/`` on ``sys.path`` and loads the root ``.env``
(so the scripts' import-time constants match what the backend's settings read).
All functions here are synchronous and blocking: call them from route handlers via
``anyio.to_thread.run_sync`` or from a background worker thread, never directly
inside ``async def``.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from ..core.config import ROOT_DIR

_SCRIPTS = ROOT_DIR / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
load_dotenv(ROOT_DIR / ".env")

import dynamic_family_grouper as dfg  # noqa: E402
import pipeline  # noqa: E402
import process_rfq_excel as pfe  # noqa: E402
import recommend_vendors as rv  # noqa: E402
import scrape_alibaba as sa  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from pipeline_errors import InputError, PipelineError  # noqa: E402

__all__ = [
    "InputError",
    "PipelineError",
    "dfg",
    "pfe",
    "pipeline",
    "rv",
    "sa",
    "inspect_workbook",
    "preview_items",
]

MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # zip-bomb guard for uploaded workbooks
OPTIONAL_COLUMNS = ("qty", "unit", "min_price", "max_price", "brand", "technical_specs")


def inspect_workbook(path: Path) -> dict[str, Any]:
    """Validate an .xlsx and return ``{sheets, sheet, items, warnings}``.

    Raises ``InputError`` for anything unusable. Never executes macros or formulas.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise InputError("Workbook is too large when decompressed")
        wb = load_workbook(filename=path, read_only=True, data_only=True)
        try:
            sheets = list(wb.sheetnames)
        finally:
            wb.close()
    except zipfile.BadZipFile as exc:
        raise InputError("File is not a valid .xlsx workbook") from exc
    except InputError:
        raise
    except Exception as exc:
        raise InputError("Could not read workbook") from exc

    if not sheets:
        raise InputError("Workbook has no sheets")

    # Use the same sheet for both loaders (pfe defaults to the active sheet, dfg to the first)
    sheet = sheets[0]
    warnings: list[str] = []
    items = pfe.load_rfq_excel(str(path), sheet_name=sheet, warnings=warnings)
    if not items:
        raise InputError("No valid product rows found in the workbook")

    present = {k for it in items for k in OPTIONAL_COLUMNS if it.get(k) not in (None, "", {})}
    for col in OPTIONAL_COLUMNS:
        if col not in present:
            warnings.append(f"Column '{col}' is missing or empty in every row")
    return {"sheets": sheets, "sheet": sheet, "items": items, "warnings": warnings}


def preview_items(items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    return [
        {
            "position": it["position"],
            "name": it["name"],
            "qty": it["qty"],
            "unit": it["unit"],
            "min_price": it["min_price"],
            "max_price": it["max_price"],
            "brand": it["brand_raw"],
        }
        for it in items[:limit]
    ]


def default_weights() -> dict[str, Optional[float]]:
    return dict(rv.RAW_WEIGHTS)
