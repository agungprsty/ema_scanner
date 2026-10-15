import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

from src.backtest.data import load_klines_csv

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    "fetch_klines", Path(__file__).resolve().parents[1] / "scripts" / "fetch_klines.py"
)
fetch_klines = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_klines)

H2 = 2 * 60 * 60 * 1000
BINANCE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)


def _kline(open_ms: int, close: str) -> str:
    return f"{open_ms},100.10,101.00,99.50,{close},12.5,{open_ms + H2 - 1},1251.25,42,6.0,600.5,0"


def _zip(name: str, lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return buf.getvalue()


def _checksum(data: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


class FakeBinanceVision:
    """Serves zips and their .CHECKSUM files by URL and records every request."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            raise FileNotFoundError(url)
        return self.files[name]


def _published(symbol: str, months: dict[str, list[str]]) -> dict[str, bytes]:
    files = {}
    for month, lines in months.items():
        name = f"{symbol}-2h-{month}.zip"
        data = _zip(name, lines)
        files[name] = data
        files[f"{name}.CHECKSUM"] = _checksum(data, name)
    return files


T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
T1 = 1_738_368_000_000  # 2025-02-01 00:00 UTC


def test_month_range_spans_years():
    assert fetch_klines.month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]
    assert fetch_klines.month_range("2025-03", "2025-03") == ["2025-03"]


def test_zip_url_points_at_usdm_monthly_klines():
    assert fetch_klines.zip_url("BTCUSDT", "2h", "2025-01") == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/2h/BTCUSDT-2h-2025-01.zip"
    )


def test_read_kline_zip_keeps_published_strings_with_or_without_header():
    with_header = _zip("X-2h-2025-01.zip", [BINANCE_HEADER, _kline(T0, "100.70"), _kline(T0 + H2, "100.80")])
    without_header = _zip("X-2h-2024-12.zip", [_kline(T0 - H2, "100.00")])
    assert fetch_klines.read_kline_zip(with_header) == [
        [str(T0), "100.10", "101.00", "99.50", "100.70", "12.5", "1251.25"],
        [str(T0 + H2), "100.10", "101.00", "99.50", "100.80", "12.5", "1251.25"],
    ]
    assert fetch_klines.read_kline_zip(without_header)[0][4] == "100.00"


def test_parse_checksum_rejects_garbage():
    digest = "a" * 64
    assert fetch_klines.parse_checksum(f"{digest}  BTCUSDT-2h-2025-01.zip\n") == digest
    with pytest.raises(fetch_klines.ChecksumError):
        fetch_klines.parse_checksum("<Error>NoSuchKey</Error>")


def test_main_merges_months_into_the_backtest_csv(tmp_path):
    server = FakeBinanceVision(
        _published(
            "BTCUSDT",
            {
                "2025-01": [BINANCE_HEADER, _kline(T0 + H2, "100.80"), _kline(T0, "100.70")],
                "2025-02": [_kline(T1, "100.90")],
            },
        )
    )
    out = tmp_path / "klines"
    assert fetch_klines.main(["--symbols", "BTCUSDT", "--start", "2025-01", "--end", "2025-02", "--out", str(out)], server) == 0

    csv_path = out / "BTCUSDT_2h.csv"
    assert csv_path.read_text().splitlines() == [
        "open_time_ms,open,high,low,close,volume,quote_volume",
        f"{T0},100.10,101.00,99.50,100.70,12.5,1251.25",
        f"{T0 + H2},100.10,101.00,99.50,100.80,12.5,1251.25",
        f"{T1},100.10,101.00,99.50,100.90,12.5,1251.25",
    ]
    df = load_klines_csv(csv_path)
    assert df["close"].tolist() == [100.7, 100.8, 100.9]
    assert (tmp_path / "klines_zip" / "BTCUSDT" / "BTCUSDT-2h-2025-02.zip.CHECKSUM").is_file()


def test_rerun_resumes_from_verified_zips(tmp_path):
    server = FakeBinanceVision(_published("ETHUSDT", {"2025-01": [_kline(T0, "1.0")], "2025-02": [_kline(T1, "2.0")]}))
    args = ["--symbols", "ETHUSDT", "--start", "2025-01", "--end", "2025-02", "--out", str(tmp_path / "k")]
    assert fetch_klines.main(args, server) == 0
    assert len(server.requests) == 4

    # A corrupted zip is fetched again; the intact month is not
    zip_dir = tmp_path / "k_zip" / "ETHUSDT"
    (zip_dir / "ETHUSDT-2h-2025-02.zip").write_bytes(b"truncated")
    server.requests.clear()
    assert fetch_klines.main(args, server) == 0
    assert [u.rsplit("/", 1)[1] for u in server.requests] == ["ETHUSDT-2h-2025-02.zip.CHECKSUM", "ETHUSDT-2h-2025-02.zip"]
    assert len((tmp_path / "k" / "ETHUSDT_2h.csv").read_text().splitlines()) == 3


def test_checksum_mismatch_fails_and_keeps_nothing(tmp_path):
    files = _published("BTCUSDT", {"2025-01": [_kline(T0, "1.0")]})
    files["BTCUSDT-2h-2025-01.zip.CHECKSUM"] = _checksum(b"something else", "BTCUSDT-2h-2025-01.zip")
    with pytest.raises(fetch_klines.ChecksumError):
        fetch_klines.ensure_month("BTCUSDT", "2h", "2025-01", tmp_path, FakeBinanceVision(files))
    assert list(tmp_path.iterdir()) == []


def test_unpublished_month_fails_unless_allowed(tmp_path, capsys):
    server = FakeBinanceVision(_published("BTCUSDT", {"2025-01": [_kline(T0, "1.0")]}))
    args = ["--symbols", "BTCUSDT", "--start", "2025-01", "--end", "2025-02", "--out", str(tmp_path / "k")]
    assert fetch_klines.main(args, server) == 1
    assert "not published for 2025-02" in capsys.readouterr().err
    assert not (tmp_path / "k" / "BTCUSDT_2h.csv").exists()
    assert fetch_klines.main([*args, "--allow-missing"], server) == 0
    assert len((tmp_path / "k" / "BTCUSDT_2h.csv").read_text().splitlines()) == 2
