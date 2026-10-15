"""Download Binance USD-M futures monthly klines from data.binance.vision into one CSV per symbol.

    python scripts/fetch_klines.py --symbols BTCUSDT ETHUSDT --start 2024-11 --end 2026-01 --out data/klines

Writes {out}/{SYMBOL}_{interval}.csv as open_time_ms,open,high,low,close,volume,quote_volume, the
format src.backtest reads. Every zip is checked against its published .CHECKSUM and kept under
--zip-dir, so a rerun (or a CI cache restore) only downloads the months it is missing.
Standard library only, so it runs before any dependency is installed.
"""

import argparse
import csv
import hashlib
import io
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
HEADER = ["open_time_ms", "open", "high", "low", "close", "volume", "quote_volume"]
# Columns of a Binance kline row kept in the CSV: open time, OHLC, volume, quote volume
KEEP = (0, 1, 2, 3, 4, 5, 7)

Fetch = Callable[[str], bytes]


class ChecksumError(Exception):
    pass


def month_range(start: str, end: str) -> list[str]:
    """Months from `start` to `end` inclusive, both YYYY-MM."""
    year, month = (int(x) for x in start.split("-"))
    last = tuple(int(x) for x in end.split("-"))
    months = []
    while (year, month) <= last:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def zip_name(symbol: str, interval: str, month: str) -> str:
    return f"{symbol}-{interval}-{month}.zip"


def zip_url(symbol: str, interval: str, month: str) -> str:
    return f"{BASE_URL}/{symbol}/{interval}/{zip_name(symbol, interval, month)}"


def parse_checksum(text: str) -> str:
    """The sha256 hex digest from a .CHECKSUM file ("<hex>  <file name>")."""
    digest = text.split()[0].strip().lower() if text.split() else ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ChecksumError(f"not a sha256 checksum: {text[:80]!r}")
    return digest


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_kline_zip(data: bytes) -> list[list[str]]:
    """Kept columns of every kline row in a monthly zip; header rows (newer files have one) skipped.

    Values stay the exact strings Binance published, so no float formatting creeps into the CSV.
    """
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            with zf.open(name) as fh:
                for record in csv.reader(io.TextIOWrapper(fh, encoding="utf-8")):
                    if not record or not record[0].strip().isdigit():
                        continue
                    rows.append([record[i].strip() for i in KEEP])
    return rows


def http_fetch(url: str, attempts: int = 3, timeout: float = 60.0) -> bytes:
    """GET `url`; FileNotFoundError on 404 (a month not published), retries other failures."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            if attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def ensure_month(symbol: str, interval: str, month: str, zip_dir: Path, fetch: Fetch = http_fetch) -> Path | None:
    """Local path of a verified monthly zip, downloading it unless a verified copy is already there.

    Returns None when Binance has not published the month. The zip is written through a temporary
    file, so an interrupted run never leaves a partial zip behind that looks complete.
    """
    zip_dir.mkdir(parents=True, exist_ok=True)
    name = zip_name(symbol, interval, month)
    zip_path = zip_dir / name
    sum_path = zip_dir / f"{name}.CHECKSUM"

    if zip_path.is_file() and sum_path.is_file():
        try:
            if sha256_hex(zip_path.read_bytes()) == parse_checksum(sum_path.read_text()):
                return zip_path
        except ChecksumError:
            pass

    url = zip_url(symbol, interval, month)
    try:
        checksum_text = fetch(f"{url}.CHECKSUM").decode()
        data = fetch(url)
    except FileNotFoundError:
        return None
    expected = parse_checksum(checksum_text)
    actual = sha256_hex(data)
    if actual != expected:
        raise ChecksumError(f"{name}: sha256 {actual} does not match published {expected}")

    tmp = zip_path.with_name(f"{name}.part")
    tmp.write_bytes(data)
    tmp.replace(zip_path)
    sum_path.write_text(checksum_text)
    return zip_path


def write_symbol_csv(zip_paths: list[Path], out_path: Path) -> int:
    """Merge monthly zips into one CSV sorted by open time, one row per candle. Returns rows written."""
    by_open: dict[int, list[str]] = {}
    for path in zip_paths:
        for row in read_kline_zip(path.read_bytes()):
            by_open[int(row[0])] = row
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f"{out_path.name}.part")
    with open(tmp, "w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        for open_ms in sorted(by_open):
            writer.writerow(by_open[open_ms])
    tmp.replace(out_path)
    return len(by_open)


def main(argv: list[str] | None = None, fetch: Fetch = http_fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="first month, YYYY-MM")
    parser.add_argument("--end", required=True, help="last month, YYYY-MM (inclusive)")
    parser.add_argument("--interval", default="2h")
    parser.add_argument("--out", type=Path, default=Path("data/klines"))
    parser.add_argument("--zip-dir", type=Path, help="where monthly zips are kept (default: <out>_zip)")
    parser.add_argument(
        "--allow-missing", action="store_true", help="skip months Binance has not published instead of failing"
    )
    args = parser.parse_args(argv)

    zip_root = args.zip_dir or args.out.with_name(f"{args.out.name}_zip")
    months = month_range(args.start, args.end)
    symbols = [s.strip().upper() for v in args.symbols for s in v.split(",") if s.strip()]
    status = 0
    for symbol in symbols:
        paths, missing = [], []
        for month in months:
            path = ensure_month(symbol, args.interval, month, zip_root / symbol, fetch)
            if path is None:
                missing.append(month)
            else:
                paths.append(path)
        if missing:
            print(f"{symbol}: not published for {', '.join(missing)}", file=sys.stderr)
            if not args.allow_missing:
                status = 1
                continue
        out_path = args.out / f"{symbol}_{args.interval}.csv"
        rows = write_symbol_csv(paths, out_path)
        print(f"{symbol}: {rows} candles from {len(paths)} months -> {out_path}")
    return status


if __name__ == "__main__":
    sys.exit(main())
