from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn


def _sanitize_sslkeylogfile_env() -> None:
    sslkeylogfile = os.getenv("SSLKEYLOGFILE")
    if not sslkeylogfile:
        return
    try:
        parent = os.path.dirname(sslkeylogfile) or "."
        os.makedirs(parent, exist_ok=True)
        with open(sslkeylogfile, "a", encoding="utf-8"):
            pass
    except Exception:
        os.environ.pop("SSLKEYLOGFILE", None)
        print(f"Disabled SSLKEYLOGFILE because the path is not writable: {sslkeylogfile}")


_sanitize_sslkeylogfile_env()

from src.api.router import router
from src.service.browser_runtime import browser_runtime
from src.service.runtime_bootstrap import bootstrap_runtime_from_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        bootstrap = await bootstrap_runtime_from_snapshot()
        restored_count = int(bootstrap.get("restored_auth_snapshot_count") or 0)
        warmed_up_count = int(bootstrap.get("warmed_up_session_count") or 0)
        if restored_count:
            print(f"Restored {restored_count} auth session snapshot(s) from session.json")
        if warmed_up_count:
            print(f"Browser-backed signer warmed up for {warmed_up_count} session(s)")
    except Exception as exc:
        print(f"Browser-backed signer warmup failed: {exc}")
    print("Session bootstrap complete")
    try:
        yield
    finally:
        await browser_runtime.close()


app = FastAPI(
    title="Doubao API Service",
    description="Lightweight Doubao API proxy service",
    version="0.2.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="src/static"), name="static")
templates = Jinja2Templates(directory="src/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


app.include_router(router, prefix="/api")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8100, reload=False)
