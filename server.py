"""
server.py
─────────
FastAPI backend for kappal auto scrapper.
• Serves the frontend at  GET  /
• Accepts scrape jobs via  WebSocket  /ws/scrape
  – Streams progress messages back in real time
  – Sends the final JSON result when done
"""

import json
import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel

from scraper import authenticate_kappal, manual_search_and_scrape_kappal, scrape_kappal

# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="kappal auto scrapper", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/export/excel")
async def export_excel(payload: dict):
    workbook = build_rates_workbook(payload)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = "kappal_rates.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── WebSocket scrape endpoint ────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    origin:           str
    destination:      str
    cut_off_date:     str
    load_type:        str  = "20GP"
    quantity:         int  = 1
    origin_service_mode: str = "CY"
    destination_service_mode: str = "CY"
    origin_carrier_sd: bool = False
    destination_carrier_sd: bool = False
    include_nearby_origin: bool = False
    include_nearby_destination: bool = False
    charges: str = "Freight, Origin, Destination +1"
    search_reference_name: str = ""
    search_currency: str = "USD"


class AuthRequest(BaseModel):
    auth_timeout: int = 300   # seconds to wait for manual login/CAPTCHA solve


class ManualScrapeRequest(BaseModel):
    search_timeout: int = 600   # seconds to wait for the user to submit search


RATE_COLUMNS = [
    "Mode", "Rate Type", "Shipping Line", "Shipping Line Code", "Container Type",
    "Service Mode", "Service Name", "POL Code", "POL Name", "Origin Terminal",
    "POD Code", "POD Name", "Destination Terminal", "Via Codes", "Transit Time",
    "Sailing Date", "Effective Period", "Cargo Type", "Commodity", "Incoterms",
    "Freight Currency", "Freight Rate", "Total Currency", "Total Rate",
    "Remarks", "Inclusions", "Source URL",
]

CHARGE_COLUMNS = [
    "Rate Row", "Section", "Charge Name", "Basis", "Equipment Type", "Quantity",
    "Unit Currency", "Unit Price", "Amount Currency", "Amount", "Comments",
]


def build_rates_workbook(payload: dict) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Rates"
    charges_ws = wb.create_sheet("Charges")

    ws.append(RATE_COLUMNS)
    charges_ws.append(CHARGE_COLUMNS)

    source_url = (payload.get("search_params") or {}).get("source_url")
    for idx, rate in enumerate(payload.get("results") or [], start=1):
        ws.append(rate_to_row(rate, source_url))
        for charge in charge_rows(rate, idx):
            charges_ws.append(charge)

    style_sheet(ws)
    style_sheet(charges_ws)
    return wb


def rate_to_row(rate: dict, source_url: str | None) -> list:
    total = amount_parts(rate.get("total_cost")) or amount_parts(rate.get("card_total_rate"))
    freight = amount_parts(rate.get("freight_subtotal")) or amount_parts(rate.get("card_freight_rate"))
    remarks = rate.get("remarks_and_inclusions") or {}
    return [
        "SEA-FCL",
        "SPOT RATE",
        rate.get("carrier") or rate.get("card_carrier"),
        carrier_code(rate.get("carrier") or rate.get("card_carrier")),
        first_equipment_type(rate),
        service_mode(rate),
        rate.get("service_name"),
        rate.get("port_of_loading") or rate.get("port_of_origin"),
        port_name_from_raw(rate.get("remarks_and_inclusions", {}).get("remarks"), "Port of Loading"),
        None,
        rate.get("port_of_discharge"),
        port_name_from_raw(rate.get("remarks_and_inclusions", {}).get("remarks"), "Port Of Discharge"),
        None,
        rate.get("transshipment_port"),
        rate.get("transit_time") or rate.get("card_transit_time"),
        rate.get("sailing_date"),
        rate.get("card_sailing_date"),
        rate.get("cargo_type") or rate.get("card_cargo_type"),
        rate.get("commodity"),
        rate.get("incoterms"),
        freight[0],
        freight[1],
        total[0],
        total[1],
        remarks.get("remarks"),
        remarks.get("inclusions"),
        source_url,
    ]


def charge_rows(rate: dict, rate_index: int) -> list:
    rows = []
    charges = rate.get("charges") or {}
    for section_name, section in charges.items():
        if not isinstance(section, dict):
            continue
        for item in section.get("line_items") or []:
            unit = amount_parts(item.get("unit_price"))
            amount = amount_parts(item.get("amount"))
            rows.append([
                rate_index,
                section_name,
                item.get("name"),
                item.get("basis"),
                item.get("equipment_type"),
                item.get("quantity"),
                unit[0],
                unit[1],
                amount[0],
                amount[1],
                item.get("comments"),
            ])
    return rows


def amount_parts(value) -> tuple:
    if isinstance(value, dict):
        return value.get("currency"), value.get("amount")
    if isinstance(value, (int, float)):
        return "USD", value
    if isinstance(value, str):
        import re
        match = re.search(r"([A-Z]{3})?\s*([\d,]+(?:\.\d+)?)", value)
        if match:
            return match.group(1), float(match.group(2).replace(",", ""))
    return None, None


def first_equipment_type(rate: dict):
    for section in (rate.get("charges") or {}).values():
        if not isinstance(section, dict):
            continue
        for item in section.get("line_items") or []:
            if item.get("equipment_type"):
                return item["equipment_type"]
    return None


def service_mode(rate: dict):
    if rate.get("origin_service_mode") and rate.get("destination_service_mode"):
        return f"{rate['origin_service_mode']}/{rate['destination_service_mode']}"
    return rate.get("service_type") or rate.get("card_service_type")


def carrier_code(name: str | None):
    if not name:
        return None
    known = {
        "OOCL": "OOCL",
        "ONE": "ONE",
        "COSCO": "COSCO",
        "Yang Ming": "YML",
        "Hapag": "HLCU",
        "Maersk": "MAEU",
        "MSC": "MSCU",
    }
    for key, value in known.items():
        if key.lower() in name.lower():
            return value
    return name.split()[0].upper()


def port_name_from_raw(raw: str | None, label: str):
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line.lower() == label.lower() and i + 1 < len(lines):
            value = lines[i + 1]
            return value.split("/", 1)[1] if "/" in value else value
    return None


def style_sheet(ws):
    header_fill = PatternFill("solid", fgColor="0B1E63")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for column in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in column)
        width = min(max(max_len + 2, 12), 50)
        ws.column_dimensions[get_column_letter(column[0].column)].width = width


@app.websocket("/ws/authenticate")
async def ws_authenticate(ws: WebSocket):
    await ws.accept()

    async def send(obj: dict):
        await ws.send_text(json.dumps(obj))

    try:
        raw = await ws.receive_text()
        req = AuthRequest(**json.loads(raw))

        async def progress(msg: str):
            await send({"type": "progress", "message": msg})

        result = await authenticate_kappal(
            progress_cb=progress,
            headless=False,
            auth_timeout=req.auth_timeout,
        )

        await send({"type": "complete", "data": result})

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError as e:
        await send({"type": "error", "message": f"Bad request JSON: {e}"})
    except Exception as e:
        await send({"type": "error", "message": str(e)})


@app.websocket("/ws/manual-scrape")
async def ws_manual_scrape(ws: WebSocket):
    await ws.accept()

    async def send(obj: dict):
        await ws.send_text(json.dumps(obj))

    try:
        raw = await ws.receive_text()
        req = ManualScrapeRequest(**json.loads(raw))

        async def progress(msg: str):
            await send({"type": "progress", "message": msg})

        result = await manual_search_and_scrape_kappal(
            progress_cb=progress,
            headless=False,
            search_timeout=req.search_timeout,
        )

        await send({"type": "complete", "data": result})

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError as e:
        await send({"type": "error", "message": f"Bad request JSON: {e}"})
    except Exception as e:
        await send({"type": "error", "message": str(e)})


@app.websocket("/ws/scrape")
async def ws_scrape(ws: WebSocket):
    """
    Protocol (JSON messages both ways):

    Client → Server:
      { "origin": ..., "destination": ..., "cut_off_date": ...,
        "load_type": ..., "quantity": ... }

    Server → Client:
      { "type": "progress", "message": "..." }
      { "type": "complete", "data": { ...results... } }
      { "type": "error",    "message": "..." }
    """
    await ws.accept()

    async def send(obj: dict):
        await ws.send_text(json.dumps(obj))

    try:
        raw = await ws.receive_text()
        req = ScrapeRequest(**json.loads(raw))

        async def progress(msg: str):
            await send({"type": "progress", "message": msg})

        result = await scrape_kappal(
            origin_query     = req.origin,
            destination_query= req.destination,
            cut_off_date     = req.cut_off_date,
            load_type        = req.load_type,
            quantity         = req.quantity,
            origin_service_mode=req.origin_service_mode,
            destination_service_mode=req.destination_service_mode,
            origin_carrier_sd=req.origin_carrier_sd,
            destination_carrier_sd=req.destination_carrier_sd,
            include_nearby_origin=req.include_nearby_origin,
            include_nearby_destination=req.include_nearby_destination,
            charges=req.charges,
            search_reference_name=req.search_reference_name,
            search_currency=req.search_currency,
            progress_cb      = progress,
            headless         = True,
        )

        await send({"type": "complete", "data": result})

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError as e:
        await send({"type": "error", "message": f"Bad request JSON: {e}"})
    except Exception as e:
        await send({"type": "error", "message": str(e)})


# ─── Run directly ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
