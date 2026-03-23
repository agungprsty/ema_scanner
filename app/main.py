from fastapi import FastAPI
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo
from app.services.scanner import scan_crossovers

# Inisialisasi Scheduler
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    print("⏰ Menghidupkan Scheduler...")
    scheduler.add_job(
        scan_crossovers,
        trigger='cron',
        hour="*",
        minute=1, # Berjalan di menit ke-1 setiap jam untuk memastikan candle closed
        timezone=ZoneInfo("Asia/Jakarta"),
        id='crossovers_scanner'
    )
    scheduler.start()
    
    yield # Aplikasi berjalan di sini
    
    # --- SHUTDOWN ---
    print("🛑 Mematikan Scheduler...")
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan, title="Crypto Crossover Bot")

@app.get("/")
def root():
    return {"status": "running", "jobs": [j.id for j in scheduler.get_jobs()]}

@app.get("/manual-scan")
def manual_scan():
    # Endpoint untuk trigger scan manual tanpa menunggu jadwal
    scan_crossovers()
    return {"message": "Scan manual telah dijalankan"}