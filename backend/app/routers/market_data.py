from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Form, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from .. import models, schemas
from ..db import get_db
from ..bars import parse_bars
from ..scid_reader import read_scid_bytes, parse_header, read_scid_path, scid_file_stats
from ..instruments import extract_root, get_spec
from sqlalchemy.dialects.postgresql import insert as pg_insert
import os
import tempfile

router = APIRouter(prefix="/api/market-data", tags=["market-data"])


@router.post("/upload", response_model=schemas.MarketDataUploadResult)
async def upload_bars(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    timeframe: str = Form("1m"),
    price_divisor: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    bars, meta = parse_bars(content, symbol, price_divisor=price_divisor, timeframe=timeframe)
    if not bars:
        raise HTTPException(400, f"No bars parsed. meta={meta}")

    # Idempotent insert: skip dupes by (symbol, timeframe, ts)
    existing_pairs = set(db.execute(
        select(models.MarketDataBar.ts).where(
            models.MarketDataBar.symbol == symbol,
            models.MarketDataBar.timeframe == timeframe,
        )
    ).scalars().all())

    inserted = 0; skipped = 0
    for b in bars:
        if b.ts in existing_pairs:
            skipped += 1; continue
        db.add(models.MarketDataBar(
            symbol=symbol, timeframe=timeframe, ts=b.ts,
            o=b.o, h=b.h, l=b.l, c=b.c, v=b.v,
            source="sierra_upload",
        ))
        existing_pairs.add(b.ts)
        inserted += 1
    db.commit()

    notes = []
    notes.append(f"Delimiter: {meta.get('delim')}")
    notes.append(f"Header detected: {meta.get('header_detected')}")
    notes.append(f"Price divisor used: {meta.get('price_divisor')}")
    if meta.get('header_detected'):
        notes.append(f"Columns: {meta.get('columns')}")

    return schemas.MarketDataUploadResult(
        symbol=symbol, timeframe=timeframe,
        parsed=len(bars), inserted=inserted, skipped_duplicates=skipped,
        price_divisor=meta.get('price_divisor', 1.0),
        earliest=min(b.ts for b in bars), latest=max(b.ts for b in bars),
        notes=notes,
    )


@router.get("/summary", response_model=list[schemas.MarketDataSummaryRow])
def summary(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            models.MarketDataBar.symbol,
            models.MarketDataBar.timeframe,
            func.count(models.MarketDataBar.id).label("n"),
            func.min(models.MarketDataBar.ts).label("earliest"),
            func.max(models.MarketDataBar.ts).label("latest"),
        ).group_by(models.MarketDataBar.symbol, models.MarketDataBar.timeframe)
        .order_by(models.MarketDataBar.symbol, models.MarketDataBar.timeframe)
    ).all()
    out = []
    for r in rows:
        sources = [s for (s,) in db.execute(
            select(models.MarketDataBar.source).where(
                models.MarketDataBar.symbol == r.symbol,
                models.MarketDataBar.timeframe == r.timeframe,
            ).distinct()
        ).all() if s]
        out.append(schemas.MarketDataSummaryRow(
            symbol=r.symbol, timeframe=r.timeframe, bar_count=r.n,
            earliest=r.earliest, latest=r.latest, sources=sources,
        ))
    return out


@router.get("/bars", response_model=list[schemas.MarketDataBarOut])
def get_bars(
    symbol: str,
    from_dt: str = Query(..., alias="from"),
    to_dt: str = Query(..., alias="to"),
    timeframe: str = "1m",
    db: Session = Depends(get_db),
):
    try:
        start = datetime.fromisoformat(from_dt)
        end = datetime.fromisoformat(to_dt)
    except ValueError:
        raise HTTPException(400, "Bad ISO datetime in from/to")
    rows = db.execute(
        select(models.MarketDataBar).where(
            models.MarketDataBar.symbol == symbol,
            models.MarketDataBar.timeframe == timeframe,
            models.MarketDataBar.ts >= start,
            models.MarketDataBar.ts <= end,
        ).order_by(models.MarketDataBar.ts)
    ).scalars().all()
    return rows


@router.post("/upload-scid", response_model=schemas.MarketDataUploadResult)
async def upload_scid(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    timeframe: str = Form("1m"),
    price_divisor: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    """Read a Sierra Chart .scid binary file directly.  Streams the upload to disk
    in 1 MB chunks and parses records without loading the whole file into memory.

    price_divisor: None = auto-detect from the instrument spec + first-record sanity check.
    """
    tmp = tempfile.NamedTemporaryFile(prefix="scid_", suffix=".scid", delete=False)
    tmp_path = tmp.name
    bytes_received = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            tmp.write(chunk)
            bytes_received += len(chunk)
        tmp.close()
        if bytes_received < 96:
            raise HTTPException(400, "File too small to be a .scid")
        try:
            stats = scid_file_stats(tmp_path)
        except ValueError as e:
            raise HTTPException(400, f"Not a valid .scid file: {e}")

        return _ingest_scid_from_path(
            db, tmp_path, symbol, timeframe, stats,
            source="scid_upload", source_label=f"uploaded ({bytes_received/1024/1024:.1f} MB)",
            price_divisor=price_divisor,
        )
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


def _auto_divisor(path: str, symbol: str) -> float:
    """Sniff the first record of a .scid file and decide whether prices need division.

    Sierra stores .scid prices as float32, but for some CME index futures (MNQ, MES, etc.)
    those values are integer ticks ×100 of the displayed price. We detect by peeking
    at the first record's close and comparing against the instrument spec's divisor.
    """
    from ..scid_reader import iter_records_path
    spec = get_spec(symbol)
    spec_div = spec.get("price_divisor", 1.0) or 1.0
    if spec_div <= 1:
        return 1.0
    try:
        first = next(iter_records_path(path))
    except StopIteration:
        return 1.0
    # If the raw value looks like an integer-tick representation (e.g. 2723050 for MNQ),
    # apply the divisor.  Otherwise leave it alone.
    if first.c > 50_000:
        return spec_div
    return 1.0


def _ingest_scid_from_path(db: Session, path: str, symbol: str, timeframe: str,
                           stats: dict, source: str, source_label: str,
                           price_divisor: Optional[float] = None,
                           ) -> schemas.MarketDataUploadResult:
    """Aggregate a .scid file into bars + bulk-upsert.  Applies price_divisor to OHLC."""
    if price_divisor is None:
        price_divisor = _auto_divisor(path, symbol)
    inv = 1.0 / price_divisor if price_divisor else 1.0

    batch: list[dict] = []
    BATCH = 5000
    inserted_total = 0
    bar_count = 0
    earliest = None
    latest = None

    def flush():
        nonlocal inserted_total, batch
        if not batch:
            return
        stmt = (
            pg_insert(models.MarketDataBar)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "ts"])
            .returning(models.MarketDataBar.id)
        )
        result = db.execute(stmt)
        inserted_total += len(result.fetchall())
        batch = []

    for b in read_scid_path(path, timeframe=timeframe):
        bar_count += 1
        if earliest is None or b.ts < earliest: earliest = b.ts
        if latest is None or b.ts > latest: latest = b.ts
        batch.append({
            "symbol": symbol, "timeframe": timeframe, "ts": b.ts,
            "o": b.o * inv, "h": b.h * inv, "l": b.l * inv, "c": b.c * inv, "v": b.v,
            "source": source,
        })
        if len(batch) >= BATCH:
            flush()
    flush()
    db.commit()
    skipped = bar_count - inserted_total

    notes = [
        f"Source: {source_label}",
        f".scid version {stats['version']}, {stats['record_count']:,} raw records, {stats['size_bytes']/1024/1024:.1f} MB",
        f"Aggregated to {bar_count:,} {timeframe} bars (inserted {inserted_total:,}, skipped {skipped:,} duplicates)",
        (f"Auto-detected price divisor {price_divisor:g} for {symbol}" if price_divisor != 1
         else "Prices read at native scale (divisor 1)"),
    ]
    return schemas.MarketDataUploadResult(
        symbol=symbol, timeframe=timeframe,
        parsed=bar_count, inserted=inserted_total, skipped_duplicates=skipped,
        price_divisor=price_divisor, earliest=earliest, latest=latest, notes=notes,
    )


SIERRA_MOUNT = os.environ.get("SIERRA_DATA_PATH", "/sierra-data")


@router.get("/sierra/files")
def list_sierra_files():
    """List .scid files in the bind-mounted Sierra Data folder (if configured).

    Mount your Sierra Data folder in docker-compose:
      volumes:
        - "/path/to/SierraChart/Data:/sierra-data:ro"
    """
    if not os.path.isdir(SIERRA_MOUNT):
        return {"mounted": False, "path": SIERRA_MOUNT, "files": [],
                "hint": "Add a bind-mount of your Sierra Chart Data folder to /sierra-data in docker-compose.yml"}
    files = []
    for name in sorted(os.listdir(SIERRA_MOUNT)):
        if name.lower().endswith(".scid"):
            full = os.path.join(SIERRA_MOUNT, name)
            try:
                st = os.stat(full)
                files.append({
                    "filename": name,
                    "size_bytes": st.st_size,
                    "modified": st.st_mtime,
                })
            except OSError:
                continue
    return {"mounted": True, "path": SIERRA_MOUNT, "files": files}


@router.post("/sierra/import", response_model=schemas.MarketDataUploadResult)
def import_sierra_file(
    filename: str = Form(...),
    symbol: str = Form(...),
    timeframe: str = Form("1m"),
    price_divisor: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    """Import a .scid file from the mounted Sierra Data folder."""
    if not os.path.isdir(SIERRA_MOUNT):
        raise HTTPException(400, "Sierra Data folder not mounted. See /api/market-data/sierra/files.")
    safe = os.path.basename(filename)
    full = os.path.join(SIERRA_MOUNT, safe)
    if not os.path.isfile(full):
        raise HTTPException(404, f"File not found: {safe}")
    try:
        stats = scid_file_stats(full)
    except ValueError as e:
        raise HTTPException(400, f"Not a valid .scid file: {e}")
    return _ingest_scid_from_path(
        db, full, symbol, timeframe, stats,
        source="scid_folder", source_label=f"/sierra-data/{safe}",
        price_divisor=price_divisor,
    )


@router.delete("/symbol/{symbol}")
def delete_symbol(symbol: str, timeframe: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.MarketDataBar).filter_by(symbol=symbol)
    if timeframe:
        q = q.filter_by(timeframe=timeframe)
    n = q.count(); q.delete(); db.commit()
    return {"deleted": n}


@router.post("/yahoo-fetch", response_model=schemas.YahooFetchResult)
def yahoo_fetch(
    symbol: str = Form(...),
    timeframe: str = Form("1m"),
    days: int = Form(7),
    db: Session = Depends(get_db),
):
    """Best-effort Yahoo Finance fetch for continuous front-month contract.
    Limitations: 1m bars only available for last ~7 days, 5m for ~60 days.
    Specific contracts (e.g. MNQM6) aren't on Yahoo — we use continuous (MNQ=F).
    """
    notes: list[str] = []
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        raise HTTPException(503, "yfinance not installed in this environment")

    root = extract_root(symbol)
    yahoo_symbol = _to_yahoo(root)
    if not yahoo_symbol:
        raise HTTPException(400, f"No Yahoo symbol mapping for root '{root}'")

    interval_map = {"1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
                    "60m": "60m", "1h": "60m", "1d": "1d"}
    yf_interval = interval_map.get(timeframe, "1m")

    try:
        df = yf.download(yahoo_symbol, period=f"{days}d", interval=yf_interval,
                         progress=False, threads=False)
    except Exception as e:
        raise HTTPException(502, f"Yahoo fetch failed: {e}")
    if df is None or df.empty:
        return schemas.YahooFetchResult(symbol=symbol, yahoo_symbol=yahoo_symbol,
                                        timeframe=timeframe, bars=0,
                                        notes=["Yahoo returned no data."])

    existing_ts = set(db.execute(
        select(models.MarketDataBar.ts).where(
            models.MarketDataBar.symbol == symbol,
            models.MarketDataBar.timeframe == timeframe,
        )
    ).scalars().all())

    inserted = 0
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        # Strip timezone — store naive UTC
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        if ts in existing_ts:
            continue
        try:
            o = float(row["Open"]); h = float(row["High"])
            l = float(row["Low"]); c = float(row["Close"])
            v = float(row.get("Volume", 0) or 0)
        except (ValueError, TypeError, KeyError):
            continue
        db.add(models.MarketDataBar(
            symbol=symbol, timeframe=timeframe, ts=ts,
            o=o, h=h, l=l, c=c, v=v, source="yahoo",
        ))
        inserted += 1
    db.commit()
    notes.append(f"Fetched {len(df)} rows from Yahoo, inserted {inserted}.")
    notes.append("Yahoo uses continuous front-month contracts; specific months unsupported.")
    return schemas.YahooFetchResult(
        symbol=symbol, yahoo_symbol=yahoo_symbol, timeframe=timeframe,
        bars=inserted, notes=notes,
    )


_YAHOO_ROOTS = {
    "MNQ": "MNQ=F", "MES": "MES=F", "MYM": "MYM=F", "M2K": "M2K=F",
    "NQ": "NQ=F", "ES": "ES=F", "YM": "YM=F", "RTY": "RTY=F",
    "GC": "GC=F", "MGC": "MGC=F", "SI": "SI=F", "SIL": "SIL=F",
    "CL": "CL=F", "MCL": "MCL=F", "NG": "NG=F",
    "MBT": "MBT=F", "MET": "MET=F",
    "6E": "6E=F", "M6E": "M6E=F",
}


def _to_yahoo(root: str) -> Optional[str]:
    return _YAHOO_ROOTS.get(root)
