import asyncio
import hashlib
import os
import time
import urllib.parse
import uuid
from typing import Any

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, Playwright, TimeoutError, async_playwright
try:
    from playwright_stealth import Stealth
except ImportError:
    Stealth = None


EDGE_EXECUTABLE_PATH = os.getenv(
    "DOUBAO_EDGE_PATH",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
WEBMSSDK_URL = "https://lf-flow-web-cdn.doubao.com/obj/flow-doubao/rc-client-security/c-webmssdk/1.0.0.43/webmssdk.es5.js"


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(cookie_header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        cookies[name] = value
    return cookies


def build_cookie_header(cookie_items: list[dict[str, Any]], domain_keyword: str) -> str:
    cookie_map: dict[str, str] = {}
    for item in cookie_items:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if not name or domain_keyword not in domain:
            continue
        cookie_map[name] = value
    return "; ".join(f"{name}={value}" for name, value in cookie_map.items())


def merge_cookie_headers(base_cookie_header: str, overlay_cookie_header: str) -> str:
    base = parse_cookie_header(base_cookie_header)
    overlay = parse_cookie_header(overlay_cookie_header)
    merged = dict(base)
    merged.update(overlay)
    return "; ".join(f"{name}={value}" for name, value in merged.items() if name)


def has_auth_cookies(cookie_header: str) -> bool:
    cookie_map = parse_cookie_header(cookie_header)
    required = ("sessionid", "sid_tt", "uid_tt", "ttwid")
    return all(str(cookie_map.get(name) or "").strip() for name in required)


def build_runtime_session_key(session) -> str:
    return hashlib.sha256(
        f"{session.aid}|{session.device_id}|{session.web_id}|{session.cookie}".encode("utf-8")
    ).hexdigest()


def build_runtime_session_identity(session) -> str:
    session_kind = "auth" if has_auth_cookies(getattr(session, "cookie", "")) else "guest"
    identity_parts = [
        str(getattr(session, "aid", "") or "").strip(),
        str(getattr(session, "device_id", "") or "").strip(),
        str(getattr(session, "web_id", "") or "").strip(),
        str(getattr(session, "tea_uuid", "") or "").strip(),
        session_kind,
    ]
    return hashlib.sha256("|".join(identity_parts).encode("utf-8")).hexdigest()


def generate_x_flow_trace() -> str:
    return f"04-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


class BrowserRuntime:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._session_key: str | None = None
        self._ms_token: str | None = None
        self._fp: str | None = None
        self._signer_ready = False
        self._last_error: str | None = None
        self._current_url: str | None = None
        self._page_title: str | None = None
        self._context_seeded_from_manual: bool | None = None
        self._manual_browser: Browser | None = None
        self._manual_context: BrowserContext | None = None
        self._manual_page: Page | None = None
        self._manual_session_key: str | None = None
        self._manual_identity_key: str | None = None
        self._manual_ms_token: str | None = None
        self._manual_fp: str | None = None
        self._manual_current_url: str | None = None
        self._manual_page_title: str | None = None
        self._manual_last_error: str | None = None
        self._manual_logged_in = False
        self._manual_chat_ready = False
        self._manual_auto_launch_identity_key: str | None = None
        self._stealth_warning_logged = False
        self._captured_hidden_requests: list[dict[str, Any]] = []
        self._captured_manual_requests: list[dict[str, Any]] = []
        self._captured_hidden_signer_calls: list[dict[str, Any]] = []
        self._captured_manual_signer_calls: list[dict[str, Any]] = []
        self._captured_hidden_request_calls: list[dict[str, Any]] = []
        self._captured_manual_request_calls: list[dict[str, Any]] = []

    async def close(self):
        async with self._lock:
            await self._close_manual_locked()
            if self._context:
                await self._context.close()
                self._context = None
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            self._page = None
            self._session_key = None
            self._ms_token = None
            self._fp = None
            self._signer_ready = False
            self._last_error = None
            self._current_url = None
            self._page_title = None
            self._context_seeded_from_manual = None
            self._manual_auto_launch_identity_key = None

    async def ensure_ready(self, session, seed_manual_storage: bool = True) -> dict[str, str | None]:
        async with self._lock:
            manual_session_compatible = bool(
                seed_manual_storage and self._is_manual_session_match_locked(session)
            )
            if manual_session_compatible and self._manual_context is not None:
                await self._merge_manual_session_state_locked(session)

            session_key = build_runtime_session_key(session)

            if self._page is not None and self._page.is_closed():
                await self._reset_context()

            if self._session_key != session_key:
                await self._reset_context()
                self._session_key = session_key

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._launch_browser()
            manual_storage_state = None
            context_seeded_from_manual = False
            if manual_session_compatible:
                manual_storage_state = await self._export_manual_storage_state_locked(session=session)
                context_seeded_from_manual = manual_storage_state is not None
            if (
                self._context is not None
                and self._context_seeded_from_manual is not None
                and self._context_seeded_from_manual != context_seeded_from_manual
            ):
                await self._reset_context()
            if self._context is None or self._page is None:
                await self._create_context(session, storage_state=manual_storage_state)
                self._context_seeded_from_manual = context_seeded_from_manual

            if session.ms_token and not self._ms_token:
                self._ms_token = session.ms_token
            if getattr(session, "fp", None) and not self._fp:
                self._fp = session.fp
            if getattr(session, "web_tab_id", None) is None:
                session.web_tab_id = self._build_web_tab_id(session)

            if not await self._has_frontier_signer():
                try:
                    await self._sync_page_state(session)
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning(f"Browser signer warm-up did not fully succeed: {exc}")
            else:
                self._signer_ready = True
                self._last_error = None
                await self._refresh_runtime_state(session)

            if self._ms_token and self._ms_token != session.ms_token:
                session.ms_token = self._ms_token
            if self._fp and self._fp != getattr(session, "fp", None):
                session.fp = self._fp

            return {
                "ms_token": self._ms_token,
                "fp": self._fp,
                "signer_ready": self._signer_ready,
            }

    async def start_manual_verification(
        self,
        session,
        conversation_id: str | None = None,
        force_new_chat: bool = False,
        seed_existing_cookies: bool = False,
        reuse_existing_window: bool = True,
        wait_for_ready: bool = False,
        wait_timeout_seconds: int = 300,
        poll_interval_ms: int = 1000,
        auto_open: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            session_key = build_runtime_session_key(session)
            session_identity_key = build_runtime_session_identity(session)

            if self._manual_page is not None and self._manual_page.is_closed():
                await self._close_manual_locked()
            live_manual_window = self._has_live_manual_window_locked()

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            reuse_existing = bool(
                reuse_existing_window
                and self._manual_browser is not None
                and self._manual_context is not None
                and self._manual_page is not None
                and not self._manual_page.is_closed()
                and self._manual_identity_key == session_identity_key
            )
            restore_saved_session = bool(
                reuse_existing_window
                and not reuse_existing
                and has_auth_cookies(getattr(session, "cookie", ""))
            )
            effective_seed_existing_cookies = bool(seed_existing_cookies or restore_saved_session)
            target_url = self._build_manual_target_url(
                session,
                conversation_id=conversation_id,
                force_new_chat=force_new_chat,
                seed_existing_cookies=effective_seed_existing_cookies,
            )
            auto_launch_blocked = bool(
                auto_open
                and not reuse_existing
                and (
                    live_manual_window
                    or self._manual_auto_launch_identity_key == session_identity_key
                )
            )

            if auto_launch_blocked:
                await self._refresh_manual_runtime_state(session)
                if not self._manual_last_error:
                    self._manual_last_error = (
                        "Automatic visible browser relaunch was skipped; "
                        "use the front-end verification button if you want to reopen it."
                    )
                return {
                    "open_url": self._manual_current_url or target_url,
                    "browser": self.get_status(),
                    "ready": self._manual_chat_ready,
                    "reused_window": False,
                    "auto_launch_blocked": True,
                    "window_available": self._has_live_manual_window_locked(),
                }

            if not reuse_existing:
                if auto_open:
                    self._manual_auto_launch_identity_key = session_identity_key
                await self._close_manual_locked()
                self._manual_session_key = session_key
                self._manual_identity_key = session_identity_key
                self._captured_manual_requests = []
                self._captured_manual_signer_calls = []

                if not seed_existing_cookies:
                    session.web_tab_id = str(uuid.uuid4())
                elif getattr(session, "web_tab_id", None) is None:
                    session.web_tab_id = self._build_web_tab_id(session)

                if self._manual_browser is None:
                    self._manual_browser = await self._launch_browser(headless=False)
                if self._manual_context is None or self._manual_page is None:
                    await self._create_manual_context(
                        session,
                        seed_existing_cookies=effective_seed_existing_cookies,
                    )
            else:
                self._manual_session_key = session_key
                self._manual_identity_key = session_identity_key

            try:
                await self._navigate_manual_verification_page_locked(session, target_url)
                self._manual_last_error = None
            except Exception as exc:
                self._manual_last_error = str(exc)
                raise RuntimeError(f"Failed to open manual verification browser: {exc}")

            await self._refresh_manual_runtime_state(session)
            if wait_for_ready:
                try:
                    await self._wait_for_manual_ready_locked(
                        session,
                        timeout_seconds=wait_timeout_seconds,
                        poll_interval_ms=poll_interval_ms,
                    )
                except Exception as exc:
                    self._manual_last_error = str(exc)
            return {
                "open_url": target_url,
                "browser": self.get_status(),
                "ready": self._manual_chat_ready,
                "reused_window": reuse_existing,
                "auto_launch_blocked": False,
                "window_available": self._has_live_manual_window_locked(),
            }

    async def finish_manual_verification(self, session, close_window: bool = True) -> dict[str, str | None]:
        async with self._lock:
            manual_page_closed = self._manual_page is not None and self._manual_page.is_closed()
            if self._manual_context is None:
                raise RuntimeError("No manual verification window is open")

            if not manual_page_closed and self._manual_page is not None:
                await self._refresh_manual_runtime_state(session)
            page_cookies = await self._manual_context.cookies(
                ["https://www.doubao.com", "https://mssdk.bytedance.com"]
            )
            cookie_header = build_cookie_header(page_cookies, "doubao.com")
            if cookie_header:
                session.cookie = merge_cookie_headers(getattr(session, "cookie", ""), cookie_header)

            cookie_map = {cookie["name"]: cookie["value"] for cookie in page_cookies if cookie.get("name")}
            session.fp = cookie_map.get("s_v_web_id") or self._manual_fp or self._fp or getattr(session, "fp", None)
            session.ms_token = (
                cookie_map.get("msToken")
                or self._manual_ms_token
                or self._ms_token
                or getattr(session, "ms_token", None)
            )

            if session.fp:
                self._fp = session.fp
            if session.ms_token:
                self._ms_token = session.ms_token
            parsed_manual_url = urllib.parse.urlparse(self._manual_current_url or "")
            path_parts = [part for part in parsed_manual_url.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] == "chat":
                current_conversation_id = path_parts[1].strip()
                if current_conversation_id and current_conversation_id not in {"chat", "0"}:
                    session.room_id = current_conversation_id

            self._manual_session_key = build_runtime_session_key(session)
            self._manual_identity_key = build_runtime_session_identity(session)

            if close_window:
                await self._close_manual_locked()

            return {
                "fp": session.fp,
                "ms_token": session.ms_token,
                "cookie": session.cookie,
            }

    def get_status(self) -> dict[str, Any]:
        return {
            "edge_executable_path": EDGE_EXECUTABLE_PATH,
            "edge_executable_exists": os.path.exists(EDGE_EXECUTABLE_PATH),
            "playwright_started": self._playwright is not None,
            "browser_started": self._browser is not None,
            "context_ready": self._context is not None,
            "page_ready": self._page is not None,
            "session_bound": self._session_key is not None,
            "signer_ready": self._signer_ready,
            "fp": self._fp,
            "ms_token": self._ms_token,
            "current_url": self._current_url,
            "page_title": self._page_title,
            "last_error": self._last_error,
            "manual_browser_started": self._manual_browser is not None,
            "manual_context_ready": self._manual_context is not None,
            "manual_page_ready": self._manual_page is not None,
            "manual_session_bound": self._manual_session_key is not None,
            "manual_fp": self._manual_fp,
            "manual_ms_token": self._manual_ms_token,
            "manual_current_url": self._manual_current_url,
            "manual_page_title": self._manual_page_title,
            "manual_last_error": self._manual_last_error,
            "manual_logged_in": self._manual_logged_in,
            "manual_chat_ready": self._manual_chat_ready,
            "manual_auto_launch_identity_key": self._manual_auto_launch_identity_key,
            "hidden_capture_count": len(self._captured_hidden_requests),
            "manual_capture_count": len(self._captured_manual_requests),
            "hidden_signer_call_count": len(self._captured_hidden_signer_calls),
            "manual_signer_call_count": len(self._captured_manual_signer_calls),
            "hidden_request_call_count": len(self._captured_hidden_request_calls),
            "manual_request_call_count": len(self._captured_manual_request_calls),
        }

    def is_manual_window_bound_to(self, session) -> bool:
        if session is None:
            return False
        if (
            self._manual_browser is None
            or self._manual_context is None
            or self._manual_page is None
            or self._manual_identity_key is None
        ):
            return False
        try:
            if self._manual_page.is_closed():
                return False
        except Exception:
            return False
        return self._manual_identity_key == build_runtime_session_identity(session)

    def get_captured_requests(self) -> dict[str, Any]:
        return {
            "hidden": list(self._captured_hidden_requests),
            "manual": list(self._captured_manual_requests),
            "hidden_signer_calls": list(self._captured_hidden_signer_calls),
            "manual_signer_calls": list(self._captured_manual_signer_calls),
            "hidden_request_calls": list(self._captured_hidden_request_calls),
            "manual_request_calls": list(self._captured_manual_request_calls),
        }

    async def sync_live_manual_session(self, session) -> bool:
        async with self._lock:
            return await self._merge_manual_session_state_locked(session)

    async def _export_manual_storage_state_locked(self, session=None) -> dict[str, Any] | None:
        if self._manual_context is None:
            return None
        if session is not None and not self._is_manual_session_match_locked(session):
            return None
        try:
            return await self._manual_context.storage_state()
        except Exception as exc:
            logger.debug(f"Failed to export manual browser storage state: {exc}")
            return None

    async def _merge_manual_session_state_locked(self, session) -> bool:
        if self._manual_context is None:
            return False
        if not self._is_manual_session_match_locked(session):
            return False

        if self._manual_page is not None and self._manual_page.is_closed():
            await self._refresh_manual_runtime_state(session)
            return False

        await self._refresh_manual_runtime_state(session)
        page_cookies = await self._manual_context.cookies(
            ["https://www.doubao.com", "https://mssdk.bytedance.com"]
        )
        cookie_header = build_cookie_header(page_cookies, "doubao.com")
        cookie_map = {cookie["name"]: cookie["value"] for cookie in page_cookies if cookie.get("name")}
        changed = False

        if cookie_header:
            merged_cookie = merge_cookie_headers(getattr(session, "cookie", ""), cookie_header)
            if merged_cookie != getattr(session, "cookie", ""):
                session.cookie = merged_cookie
                changed = True

        next_fp = cookie_map.get("s_v_web_id") or self._manual_fp or getattr(session, "fp", None)
        if next_fp and next_fp != getattr(session, "fp", None):
            session.fp = next_fp
            changed = True

        next_ms_token = (
            cookie_map.get("msToken")
            or self._manual_ms_token
            or self._ms_token
            or getattr(session, "ms_token", None)
        )
        if next_ms_token and next_ms_token != getattr(session, "ms_token", None):
            session.ms_token = next_ms_token
            changed = True

        parsed_manual_url = urllib.parse.urlparse(self._manual_current_url or "")
        path_parts = [part for part in parsed_manual_url.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0] == "chat":
            current_conversation_id = path_parts[1].strip()
            if current_conversation_id and current_conversation_id not in {"chat", "0"}:
                if current_conversation_id != getattr(session, "room_id", None):
                    session.room_id = current_conversation_id
                    changed = True

        if session.fp:
            self._fp = session.fp
        if session.ms_token:
            self._ms_token = session.ms_token
        self._manual_session_key = build_runtime_session_key(session)
        self._manual_identity_key = build_runtime_session_identity(session)

        return changed

    async def send_manual_prompt(
        self,
        session,
        prompt: str,
        wait_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._manual_page is None or self._manual_context is None or self._manual_page.is_closed():
                raise RuntimeError("No manual verification window is open")

            await self._refresh_manual_runtime_state(session)
            if not self._manual_chat_ready:
                raise RuntimeError("Manual browser is not ready for chat sending")

            page = self._manual_page
            await page.bring_to_front()
            before_count = len(self._captured_manual_requests)
            start_at = int(time.time() * 1000)
            start_manual_url = str(self._manual_current_url or page.url or "")

            typed = False
            textarea = page.locator("textarea")
            try:
                textarea_count = await textarea.count()
            except Exception:
                textarea_count = 0
            if textarea_count > 0:
                for index in range(textarea_count - 1, -1, -1):
                    locator = textarea.nth(index)
                    try:
                        if await locator.is_visible():
                            await locator.click()
                            await locator.fill(prompt)
                            typed = True
                            break
                    except Exception:
                        continue

            if not typed:
                typed = bool(await page.evaluate(
                    """
                    (text) => {
                      const candidates = Array.from(
                        document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"]')
                      );
                      const visible = candidates.filter((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                      });
                      const target = visible.at(-1) || candidates.at(-1);
                      if (!target) return false;
                      target.focus();
                      const range = document.createRange();
                      range.selectNodeContents(target);
                      const sel = window.getSelection();
                      sel.removeAllRanges();
                      sel.addRange(range);
                      try {
                        document.execCommand('insertText', false, text);
                      } catch (e) {
                        target.textContent = text;
                      }
                      target.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
                      target.dispatchEvent(new Event('change', { bubbles: true }));
                      return true;
                    }
                    """,
                    prompt,
                ))

            if not typed:
                raise RuntimeError("Could not locate a visible chat input in the manual browser")

            sent = False
            send_button_selectors = [
                'button[type="submit"]',
                'button[aria-label*="发送"]',
                'button[aria-label*="Send"]',
                'button[data-testid*="send"]',
            ]
            for selector in send_button_selectors:
                locator = page.locator(selector)
                try:
                    button_count = await locator.count()
                except Exception:
                    button_count = 0
                if button_count <= 0:
                    continue
                for index in range(button_count - 1, -1, -1):
                    button = locator.nth(index)
                    try:
                        if await button.is_visible():
                            await button.click()
                            sent = True
                            break
                    except Exception:
                        continue
                if sent:
                    break

            if not sent:
                try:
                    await page.keyboard.press("Enter")
                    sent = True
                except Exception as exc:
                    raise RuntimeError(f"Failed to trigger send in the manual browser: {exc}")

            deadline = asyncio.get_running_loop().time() + max(1, int(wait_timeout_seconds or 30))
            while True:
                await self._refresh_manual_runtime_state(session)
                latest_chat_capture = None
                for item in reversed(self._captured_manual_requests):
                    if not isinstance(item, dict):
                        continue
                    if item.get("key") != "chat_completion":
                        continue
                    captured_at_ms = int(item.get("captured_at_ms") or 0)
                    if captured_at_ms >= start_at:
                        latest_chat_capture = item
                        break

                if latest_chat_capture is not None:
                    settle_deadline = min(
                        deadline,
                        asyncio.get_running_loop().time() + 2.5,
                    )
                    while asyncio.get_running_loop().time() < settle_deadline:
                        await asyncio.sleep(0.25)
                        await self._refresh_manual_runtime_state(session)
                        current_manual_url = str(self._manual_current_url or "")
                        if current_manual_url and current_manual_url != start_manual_url:
                            break
                    return {
                        "sent": True,
                        "capture_count_before": before_count,
                        "capture_count_after": len(self._captured_manual_requests),
                        "chat_completion": latest_chat_capture,
                        "browser": self.get_status(),
                    }

                if asyncio.get_running_loop().time() >= deadline:
                    return {
                        "sent": sent,
                        "capture_count_before": before_count,
                        "capture_count_after": len(self._captured_manual_requests),
                        "chat_completion": None,
                        "browser": self.get_status(),
                    }

                await asyncio.sleep(0.5)

    async def sign_query(
        self,
        session,
        query_string: str,
        seed_manual_storage: bool = True,
    ) -> tuple[str, str | None]:
        await self.ensure_ready(session, seed_manual_storage=seed_manual_storage)
        assert self._page is not None
        sanitized_query = urllib.parse.urlencode(
            [
                (key, value)
                for key, value in urllib.parse.parse_qsl(str(query_string or ""), keep_blank_values=True)
                if key and key != "a_bogus"
            ]
        )
        query_string = sanitized_query
        if not await self._has_frontier_signer():
            await self._sync_page_state(session, force_reload=True)
        if not await self._has_frontier_signer():
            await self._reset_context()
            self._session_key = build_runtime_session_key(session)
            if self._browser is None:
                self._browser = await self._launch_browser()
            manual_storage_state = None
            if seed_manual_storage and self._is_manual_session_match_locked(session):
                manual_storage_state = await self._export_manual_storage_state_locked(session=session)
            await self._create_context(session, storage_state=manual_storage_state)
            self._context_seeded_from_manual = manual_storage_state is not None
            await self._sync_page_state(session, force_reload=True)
        if not await self._has_frontier_signer():
            raise RuntimeError(
                "frontierSign is still unavailable after webmssdk injection"
                + (f" ({self._last_error})" if self._last_error else "")
            )

        await self._install_signer_hook(self._page, manual=False)
        signature = await self._page.evaluate(
            """
            async (query) => {
              const signer =
                window.byted_acrawler?.frontierSign ||
                window.byted_acrawler?.sign ||
                globalThis.byted_acrawler?.frontierSign ||
                globalThis.byted_acrawler?.sign;
              if (typeof signer !== "function") {
                return null;
              }
              return await signer(query);
            }
            """,
            query_string,
        )
        if isinstance(signature, dict):
            bogus = signature.get("a_bogus") or signature.get("X-Bogus")
        elif isinstance(signature, str):
            bogus = signature
        else:
            bogus = None

        if not bogus:
            raise RuntimeError(
                "frontierSign did not return a_bogus"
                + (f" ({self._last_error})" if self._last_error else "")
            )

        await self._refresh_signer_calls(self._page, manual=False)
        return f"{query_string}&a_bogus={bogus}", bogus

    async def fetch_text(
        self,
        session,
        url: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        referer: str | None = None,
        prefer_manual: bool = False,
        seed_manual_storage: bool = True,
    ) -> dict[str, Any]:
        manual = False
        page = None

        if (
            prefer_manual
            and self._manual_page is not None
            and self._manual_context is not None
            and not self._manual_page.is_closed()
        ):
            manual = True
            page = self._manual_page
            await self._refresh_manual_runtime_state(session)
        else:
            await self.ensure_ready(session, seed_manual_storage=seed_manual_storage)
            assert self._page is not None
            page = self._page

        payload = await page.evaluate(
            """
            async (args) => {
              const response = await fetch(args.url, {
                method: "POST",
                mode: "cors",
                credentials: "include",
                headers: args.headers || {},
                body: args.body ? JSON.stringify(args.body) : undefined,
                referrer: args.referer || undefined,
                referrerPolicy: "strict-origin-when-cross-origin",
              });
              const text = await response.text();
              const responseHeaders = {};
              response.headers.forEach((value, key) => {
                responseHeaders[key] = value;
              });
              return {
                status: response.status,
                ok: response.ok,
                text,
                headers: responseHeaders,
                url: response.url,
              };
            }
            """,
            {
                "url": url,
                "headers": headers or {},
                "body": body,
                "referer": referer,
            },
        )

        if isinstance(payload, dict):
            response_headers = payload.get("headers") or {}
            if isinstance(response_headers, dict):
                ms_token = response_headers.get("x-ms-token")
                if ms_token:
                    if manual:
                        self._manual_ms_token = ms_token
                    self._ms_token = ms_token
                    session.ms_token = ms_token
            latest_fp = self._manual_fp if manual else self._fp
            if latest_fp and latest_fp != getattr(session, "fp", None):
                session.fp = latest_fp
            if manual:
                await self._refresh_manual_runtime_state(session)
        return payload

    async def _launch_browser(self, headless: bool = True) -> Browser:
        assert self._playwright is not None
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if os.path.exists(EDGE_EXECUTABLE_PATH):
            browser_mode = "hidden" if headless else "visible"
            logger.info(f"Launching system Edge for {browser_mode} browser-backed requests: {EDGE_EXECUTABLE_PATH}")
            return await self._playwright.chromium.launch(
                executable_path=EDGE_EXECUTABLE_PATH,
                headless=headless,
                args=launch_args,
            )
        logger.warning("System Edge not found, falling back to Playwright Chromium")
        return await self._playwright.chromium.launch(headless=headless, args=launch_args)

    async def _create_context(self, session, storage_state: dict[str, Any] | None = None):
        assert self._browser is not None
        self._context = await self._browser.new_context(
            user_agent=BROWSER_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1365, "height": 900},
            screen={"width": 1365, "height": 900},
            color_scheme="light",
            storage_state=storage_state,
        )
        self._page = await self._context.new_page()
        self._context_seeded_from_manual = storage_state is not None
        self._captured_hidden_requests = []
        self._captured_hidden_signer_calls = []
        self._captured_hidden_request_calls = []
        await self._configure_page(
            self._context,
            self._page,
            session,
            self._handle_response,
            manual=False,
        )

    async def _create_manual_context(self, session, seed_existing_cookies: bool = False):
        assert self._manual_browser is not None
        self._manual_context = await self._manual_browser.new_context(
            user_agent=BROWSER_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1365, "height": 900},
            screen={"width": 1365, "height": 900},
            color_scheme="light",
        )
        await self._create_manual_page_locked(session, seed_existing_cookies=seed_existing_cookies)

    async def _create_manual_page_locked(self, session, seed_existing_cookies: bool = False):
        assert self._manual_context is not None
        if self._manual_page is not None:
            try:
                if not self._manual_page.is_closed():
                    await self._manual_page.close()
            except Exception:
                pass
        self._manual_page = await self._manual_context.new_page()
        self._captured_manual_requests = []
        self._captured_manual_signer_calls = []
        self._captured_manual_request_calls = []
        await self._configure_page(
            self._manual_context,
            self._manual_page,
            session,
            self._handle_manual_response,
            seed_cookies=seed_existing_cookies,
            manual=True,
        )

    def _has_live_manual_window_locked(self) -> bool:
        if self._manual_browser is None or self._manual_context is None or self._manual_page is None:
            return False
        try:
            return not self._manual_page.is_closed()
        except Exception:
            return False

    def _build_manual_target_url(
        self,
        session,
        *,
        conversation_id: str | None = None,
        force_new_chat: bool = False,
        seed_existing_cookies: bool = False,
    ) -> str:
        target_url = "https://www.doubao.com/chat/"
        if conversation_id and conversation_id not in {"", "0"}:
            return f"https://www.doubao.com/chat/{conversation_id}"
        if (not force_new_chat) and seed_existing_cookies and getattr(session, "room_id", None) not in (None, "", "0"):
            return f"https://www.doubao.com/chat/{session.room_id}"
        return target_url

    @staticmethod
    def _is_retryable_manual_navigation_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return any(
            marker in message
            for marker in (
                "err_aborted",
                "frame was detached",
                "navigation interrupted",
                "execution context was destroyed",
                "target page, context or browser has been closed",
                "target closed",
            )
        )

    async def _navigate_manual_verification_page_locked(self, session, target_url: str):
        if self._manual_context is None:
            raise RuntimeError("Manual browser context is not ready")

        bootstrap_url = "https://www.doubao.com/chat/"
        last_exc: Exception | None = None

        for attempt in range(2):
            if self._manual_page is None or self._manual_page.is_closed():
                await self._create_manual_page_locked(session, seed_existing_cookies=False)

            page = self._manual_page
            assert page is not None

            try:
                await page.goto(bootstrap_url, wait_until="commit", timeout=60000)
                await page.bring_to_front()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass

                if target_url != bootstrap_url:
                    await self._force_manual_redirect_locked(page, target_url)

                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                return
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and self._is_retryable_manual_navigation_error(exc):
                    logger.warning(
                        f"Manual verification navigation failed once with a transient browser error; "
                        f"recreating the page and retrying: {exc}"
                    )
                    await self._create_manual_page_locked(session, seed_existing_cookies=False)
                    continue
                raise

        if last_exc is not None:
            raise last_exc

    async def _force_manual_redirect_locked(self, page: Page, target_url: str):
        try:
            await page.evaluate(
                """
                (nextUrl) => {
                  const currentUrl = String(window.location.href || "");
                  if (currentUrl !== nextUrl) {
                    window.location.replace(nextUrl);
                  }
                }
                """,
                target_url,
            )
        except Exception as exc:
            if not self._is_retryable_manual_navigation_error(exc):
                raise

        try:
            await page.wait_for_url(
                lambda url: (
                    str(url).startswith(target_url)
                    or "/security/" in str(url)
                    or "/passport/" in str(url)
                    or "/login" in str(url)
                    or "/signup" in str(url)
                    or "/register" in str(url)
                ),
                timeout=30000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

    async def _configure_page(
        self,
        context: BrowserContext,
        page: Page,
        session,
        response_handler,
        seed_cookies: bool = True,
        manual: bool = False,
    ):
        if Stealth is not None:
            stealth = Stealth(
                navigator_languages_override=("zh-CN", "zh"),
                navigator_platform_override="Win32",
                navigator_user_agent_override=BROWSER_UA,
            )
            await stealth.apply_stealth_async(page)
        elif not self._stealth_warning_logged:
            logger.warning("playwright-stealth is not installed; browser signer detection may be less reliable")
            self._stealth_warning_logged = True
        page.on("response", response_handler)
        page.on("response", lambda response: asyncio.create_task(self._capture_exchange(response, manual=manual)))
        await page.add_init_script(
            """
            window.__codexSignerCalls = [];
            window.__codexRequestCalls = [];
            Object.defineProperty(navigator, "webdriver", {
              get: () => undefined,
            });
            Object.defineProperty(navigator, "languages", {
              get: () => ["zh-CN", "zh", "en-US", "en"],
            });
            Object.defineProperty(navigator, "platform", {
              get: () => "Win32",
            });
            Object.defineProperty(navigator, "plugins", {
              get: () => [1, 2, 3, 4, 5],
            });
            window.chrome = window.chrome || { runtime: {} };
            const originalQuery = window.navigator.permissions?.query?.bind(window.navigator.permissions);
            if (originalQuery) {
              window.navigator.permissions.query = (parameters) => {
                if (parameters && parameters.name === "notifications") {
                  return Promise.resolve({ state: Notification.permission });
                }
                return originalQuery(parameters);
              };
            }
            const shouldCaptureCodexUrl = (url) => {
              if (typeof url !== "string" || !url) return false;
              return (
                url.includes("/chat/completion") ||
                url.includes("/biz/onboarding/get_nexpulse") ||
                url.includes("/alice/profile/self") ||
                url.includes("/alice/user/launch") ||
                url.includes("/im/message/send_rate_limit") ||
                url.includes("/im/chain/single")
              );
            };
            const toSafeString = (value) => {
              try {
                if (value == null) return "";
                if (typeof value === "string") return value;
                if (typeof URLSearchParams !== "undefined" && value instanceof URLSearchParams) {
                  return value.toString();
                }
                if (typeof FormData !== "undefined" && value instanceof FormData) {
                  return "[form-data]";
                }
                if (typeof value === "object") return JSON.stringify(value);
                return String(value);
              } catch (e) {
                return "[unserializable]";
              }
            };
            const toHeadersObject = (headers) => {
              try {
                if (!headers) return {};
                if (typeof Headers !== "undefined" && headers instanceof Headers) {
                  return Object.fromEntries(headers.entries());
                }
                if (Array.isArray(headers)) {
                  return Object.fromEntries(headers);
                }
                if (typeof headers === "object") {
                  return Object.fromEntries(Object.entries(headers));
                }
              } catch (e) {}
              return {};
            };
            const pushCodexRequestCall = (entry) => {
              try {
                const normalized = {
                  ...entry,
                  stack: String(entry.stack || ""),
                  body: String(entry.body || ""),
                  url: String(entry.url || ""),
                  method: String(entry.method || "GET"),
                  at: Number(entry.at || Date.now()),
                };
                window.__codexRequestCalls.push(normalized);
                window.__codexRequestCalls = window.__codexRequestCalls.slice(-80);
              } catch (e) {}
            };
            const extractUrl = (input) => {
              if (typeof input === "string") return input;
              if (input && typeof input.url === "string") return input.url;
              return String(input || "");
            };
            const originalFetch = window.fetch?.bind(window);
            if (typeof originalFetch === "function" && !window.__codexFetchWrapped) {
              window.fetch = async function(input, init) {
                const url = extractUrl(input);
                if (shouldCaptureCodexUrl(url)) {
                  pushCodexRequestCall({
                    kind: "fetch",
                    url,
                    method: (init && init.method) || (input && input.method) || "GET",
                    headers: toHeadersObject((init && init.headers) || (input && input.headers)),
                    body: toSafeString(init && init.body),
                    stack: (new Error("codex-fetch")).stack || "",
                    at: Date.now(),
                    href: window.location.href,
                  });
                }
                return await originalFetch(input, init);
              };
              window.__codexFetchWrapped = true;
            }
            const xhrOpen = XMLHttpRequest.prototype.open;
            const xhrSend = XMLHttpRequest.prototype.send;
            if (!XMLHttpRequest.prototype.__codexWrapped) {
              XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                this.__codexMeta = {
                  method: String(method || "GET"),
                  url: String(url || ""),
                };
                return xhrOpen.call(this, method, url, ...rest);
              };
              XMLHttpRequest.prototype.send = function(body) {
                const meta = this.__codexMeta || {};
                if (shouldCaptureCodexUrl(meta.url)) {
                  pushCodexRequestCall({
                    kind: "xhr",
                    url: meta.url,
                    method: meta.method || "GET",
                    body: toSafeString(body),
                    stack: (new Error("codex-xhr")).stack || "",
                    at: Date.now(),
                    href: window.location.href,
                  });
                }
                return xhrSend.call(this, body);
              };
              XMLHttpRequest.prototype.__codexWrapped = true;
            }
            """
        )
        if seed_cookies:
            await context.add_cookies(self._build_cookie_items(session))

    async def _sync_page_state(self, session, force_reload: bool = False):
        assert self._page is not None
        self._last_error = None
        attempts: list[tuple[str, str]] = []
        candidate_urls: list[str] = []
        manual_url = str(self._manual_current_url or "").strip()
        if manual_url.startswith("https://www.doubao.com/chat"):
            candidate_urls.append(manual_url)
        room_id = str(getattr(session, "room_id", "") or "").strip()
        if room_id and room_id not in {"0", "chat"}:
            candidate_urls.append(f"https://www.doubao.com/chat/{room_id}")
        candidate_urls.extend(
            [
                "https://www.doubao.com/chat/",
                "https://www.doubao.com/",
            ]
        )
        seen_attempts: set[tuple[str, str]] = set()
        for url in candidate_urls:
            for wait_until in ("domcontentloaded", "load"):
                attempt = (url, wait_until)
                if attempt in seen_attempts:
                    continue
                seen_attempts.add(attempt)
                attempts.append(attempt)
        errors: list[str] = []

        if not force_reload and await self._has_frontier_signer():
            self._signer_ready = True
            await self._install_signer_hook(self._page, manual=False)
            await self._refresh_runtime_state(session)
            return

        for url, wait_until in attempts:
            try:
                await self._page.goto(url, wait_until=wait_until, timeout=60000)
            except TimeoutError as exc:
                errors.append(f"goto({wait_until}) timeout on {url}: {exc}")
            except Exception as exc:
                errors.append(f"goto failed on {url}: {exc}")

            try:
                await self._page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            try:
                await self._page.wait_for_function(
                    "() => typeof window.byted_acrawler?.frontierSign === 'function'",
                    timeout=12000,
                )
            except TimeoutError as exc:
                errors.append(f"frontierSign wait timeout on {url}: {exc}")
            except Exception as exc:
                errors.append(f"frontierSign wait failed on {url}: {exc}")

            if await self._has_frontier_signer():
                self._signer_ready = True
                await self._install_signer_hook(self._page, manual=False)
                await self._refresh_runtime_state(session)
                return

            try:
                await self._inject_webmssdk()
                await self._page.wait_for_function(
                    "() => typeof window.byted_acrawler?.frontierSign === 'function'",
                    timeout=12000,
                )
            except TimeoutError as exc:
                errors.append(f"webmssdk injection wait timeout on {url}: {exc}")
            except Exception as exc:
                errors.append(f"webmssdk injection failed on {url}: {exc}")

            if await self._has_frontier_signer():
                self._signer_ready = True
                self._last_error = None
                await self._install_signer_hook(self._page, manual=False)
                await self._refresh_runtime_state(session)
                return

        self._signer_ready = False
        await self._refresh_runtime_state(session)
        self._last_error = " | ".join(errors[-4:]) or "frontierSign was not available after warm-up"
        raise RuntimeError(self._last_error)

    def _build_cookie_items(self, session) -> list[dict[str, Any]]:
        cookie_map = parse_cookie_header(session.cookie)
        cookies: list[dict[str, Any]] = []
        for name, value in cookie_map.items():
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".doubao.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                }
            )
        if session.ms_token:
            cookies.append(
                {
                    "name": "msToken",
                    "value": session.ms_token,
                    "domain": ".bytedance.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                }
            )
        return cookies

    async def _reset_context(self):
        if self._context:
            await self._context.close()
        self._context = None
        self._page = None
        self._context_seeded_from_manual = None
        self._ms_token = None
        self._fp = None
        self._signer_ready = False
        self._last_error = None
        self._current_url = None
        self._page_title = None

    def _handle_response(self, response):
        try:
            ms_token = response.headers.get("x-ms-token")
            if ms_token:
                self._ms_token = ms_token
        except Exception as exc:
            logger.debug(f"Failed to capture browser response headers: {exc}")

    def _handle_manual_response(self, response):
        try:
            ms_token = response.headers.get("x-ms-token")
            if ms_token:
                self._manual_ms_token = ms_token
                self._ms_token = ms_token
        except Exception as exc:
            logger.debug(f"Failed to capture manual browser response headers: {exc}")

    def _capture_key_for_url(self, url: str) -> str | None:
        normalized = str(url or "")
        if "/chat/completion" in normalized:
            return "chat_completion"
        if "/biz/onboarding/get_nexpulse" in normalized:
            return "get_nexpulse"
        if "/alice/profile/self" in normalized:
            return "alice_profile_self"
        if "/alice/user/launch" in normalized:
            return "alice_user_launch"
        if "/im/message/send_rate_limit" in normalized:
            return "send_rate_limit"
        if "/im/chain/single" in normalized:
            return "chain_single"
        return None

    def _append_capture(self, capture: dict[str, Any], manual: bool):
        target = self._captured_manual_requests if manual else self._captured_hidden_requests
        target.append(capture)
        if len(target) > 20:
            del target[:-20]

    async def _capture_exchange(self, response, manual: bool = False):
        key = self._capture_key_for_url(getattr(response, "url", ""))
        if not key:
            return

        try:
            request = response.request
            capture = {
                "key": key,
                "source": "manual" if manual else "hidden",
                "url": getattr(request, "url", getattr(response, "url", "")),
                "method": getattr(request, "method", ""),
                "request_headers": dict(getattr(request, "headers", {}) or {}),
                "request_post_data": getattr(request, "post_data", None),
                "response_status": getattr(response, "status", None),
                "response_headers": dict(getattr(response, "headers", {}) or {}),
                "captured_at_ms": int(time.time() * 1000),
            }
            try:
                if key == "chat_completion":
                    capture["response_text"] = await asyncio.wait_for(response.text(), timeout=45)
                else:
                    capture["response_text"] = await response.text()
            except Exception as exc:
                capture["response_text_error"] = str(exc)
            self._append_capture(capture, manual=manual)
        except Exception as exc:
            logger.debug(f"Failed to capture browser exchange: {exc}")

    async def _install_signer_hook(self, page: Page, manual: bool = False):
        try:
            await page.evaluate(
                """
                () => {
                  const root =
                    window.byted_acrawler ||
                    globalThis.byted_acrawler;
                  if (!root) {
                    return false;
                  }
                  window.__codexSignerCalls = Array.isArray(window.__codexSignerCalls)
                    ? window.__codexSignerCalls
                    : [];
                  const wrap = (name) => {
                    const fn = root[name];
                    if (typeof fn !== "function" || fn.__codexWrapped) {
                      return false;
                    }
                    const wrapped = async function (...args) {
                      const result = await fn.apply(this, args);
                      try {
                        const toSafeString = (value) => {
                          if (typeof value === "string") return value;
                          return JSON.stringify(value);
                        };
                        window.__codexSignerCalls.push({
                          name,
                          args: args.map(toSafeString),
                          result: toSafeString(result),
                          at: Date.now(),
                        });
                        window.__codexSignerCalls = window.__codexSignerCalls.slice(-20);
                      } catch (e) {}
                      return result;
                    };
                    wrapped.__codexWrapped = true;
                    root[name] = wrapped;
                    return true;
                  };
                  wrap("frontierSign");
                  wrap("sign");
                  return true;
                }
                """
            )
            calls = await page.evaluate("() => window.__codexSignerCalls || []")
            if isinstance(calls, list):
                if manual:
                    self._captured_manual_signer_calls = calls[-20:]
                else:
                    self._captured_hidden_signer_calls = calls[-20:]
        except Exception as exc:
            logger.debug(f"Failed to install signer hook: {exc}")

    async def _refresh_request_calls(self, page: Page | None, manual: bool = False):
        if page is None:
            return
        try:
            calls = await page.evaluate("() => window.__codexRequestCalls || []")
            if isinstance(calls, list):
                if manual:
                    self._captured_manual_request_calls = calls[-80:]
                else:
                    self._captured_hidden_request_calls = calls[-80:]
        except Exception as exc:
            logger.debug(f"Failed to refresh request calls: {exc}")

    async def _refresh_signer_calls(self, page: Page | None, manual: bool = False):
        if page is None:
            return
        try:
            calls = await page.evaluate("() => window.__codexSignerCalls || []")
            if isinstance(calls, list):
                if manual:
                    self._captured_manual_signer_calls = calls[-20:]
                else:
                    self._captured_hidden_signer_calls = calls[-20:]
        except Exception as exc:
            logger.debug(f"Failed to refresh signer calls: {exc}")

    async def _has_frontier_signer(self) -> bool:
        if self._page is None:
            return False
        try:
            result = await self._page.evaluate(
                "() => typeof window.byted_acrawler?.frontierSign === 'function'"
            )
            return bool(result)
        except Exception:
            return False

    async def _refresh_runtime_state(self, session):
        self._signer_ready = await self._has_frontier_signer()
        await self._refresh_signer_calls(self._page, manual=False)
        await self._refresh_request_calls(self._page, manual=False)
        if self._context is not None:
            page_cookies = await self._context.cookies(
                ["https://www.doubao.com", "https://mssdk.bytedance.com"]
            )
            cookie_map = {cookie["name"]: cookie["value"] for cookie in page_cookies if cookie.get("name")}
            self._fp = cookie_map.get("s_v_web_id") or self._fp or parse_cookie_header(session.cookie).get("s_v_web_id")
            self._ms_token = cookie_map.get("msToken") or self._ms_token or session.ms_token

        if self._page is not None:
            try:
                self._current_url = self._page.url
            except Exception:
                self._current_url = None
            try:
                self._page_title = await self._page.title()
            except Exception:
                self._page_title = None

    async def _refresh_manual_runtime_state(self, session):
        cookie_map: dict[str, str] = {}
        if self._manual_page is not None:
            await self._install_signer_hook(self._manual_page, manual=True)
        await self._refresh_signer_calls(self._manual_page, manual=True)
        await self._refresh_request_calls(self._manual_page, manual=True)
        if self._manual_context is not None:
            page_cookies = await self._manual_context.cookies(
                ["https://www.doubao.com", "https://mssdk.bytedance.com"]
            )
            cookie_map = {cookie["name"]: cookie["value"] for cookie in page_cookies if cookie.get("name")}
            self._manual_fp = cookie_map.get("s_v_web_id") or self._manual_fp or parse_cookie_header(session.cookie).get("s_v_web_id")
            self._manual_ms_token = cookie_map.get("msToken") or self._manual_ms_token or session.ms_token

        if self._manual_page is not None:
            try:
                self._manual_current_url = self._manual_page.url
            except Exception:
                self._manual_current_url = None
            try:
                self._manual_page_title = await self._manual_page.title()
            except Exception:
                self._manual_page_title = None

        self._manual_logged_in = any(
            cookie_map.get(name) for name in ("sessionid", "sessionid_ss", "sid_tt", "uid_tt")
        )
        if not self._manual_logged_in:
            for capture in reversed(self._captured_manual_requests):
                if not isinstance(capture, dict):
                    continue
                if capture.get("response_status") != 200:
                    continue
                response_headers = capture.get("response_headers")
                if not isinstance(response_headers, dict):
                    continue
                if str(response_headers.get("x-tt-agw-login") or "").strip() in {"1", "true", "True"}:
                    self._manual_logged_in = True
                    break
        manual_url = str(self._manual_current_url or "")
        blocked = any(
            flag in manual_url
            for flag in ("/security/", "/passport/", "/login", "/signup", "/register", "from_logout=1")
        )
        self._manual_chat_ready = ("/chat" in manual_url) and not blocked
        if not self._manual_chat_ready:
            for capture in reversed(self._captured_manual_requests):
                if not isinstance(capture, dict):
                    continue
                if capture.get("key") != "chat_completion" or capture.get("response_status") != 200:
                    continue
                self._manual_chat_ready = True
                self._manual_logged_in = True
                break

    async def _wait_for_manual_ready_locked(
        self,
        session,
        timeout_seconds: int = 300,
        poll_interval_ms: int = 1000,
    ):
        if self._manual_context is None:
            raise RuntimeError("No manual verification window is open")

        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds or 300))
        interval_seconds = max(0.2, int(poll_interval_ms or 1000) / 1000)

        while True:
            if self._manual_page is not None and self._manual_page.is_closed():
                await self._refresh_manual_runtime_state(session)
                if self._manual_chat_ready or self._manual_logged_in:
                    return
                raise RuntimeError("Manual verification window was closed before login or verification completed")

            await self._refresh_manual_runtime_state(session)
            if self._manual_chat_ready:
                self._manual_last_error = None
                return

            if asyncio.get_running_loop().time() >= deadline:
                self._manual_last_error = (
                    f"Timed out waiting for manual verification to complete; "
                    f"url={self._manual_current_url or '-'}, logged_in={self._manual_logged_in}"
                )
                raise RuntimeError(self._manual_last_error)

            await asyncio.sleep(interval_seconds)

    async def _close_manual_locked(self):
        if self._manual_context:
            await self._manual_context.close()
            self._manual_context = None
        if self._manual_browser:
            await self._manual_browser.close()
            self._manual_browser = None
        self._manual_page = None
        self._manual_session_key = None
        self._manual_identity_key = None
        self._manual_ms_token = None
        self._manual_fp = None
        self._manual_current_url = None
        self._manual_page_title = None
        self._manual_last_error = None
        self._manual_logged_in = False
        self._manual_chat_ready = False
        self._captured_manual_requests = []
        self._captured_manual_signer_calls = []
        self._captured_manual_request_calls = []

    def _is_manual_session_match_locked(self, session) -> bool:
        if (
            session is None
            or self._manual_identity_key is None
            or self._manual_context is None
            or self._manual_page is None
        ):
            return False
        try:
            if self._manual_page.is_closed():
                return False
        except Exception:
            return False
        return self._manual_identity_key == build_runtime_session_identity(session)

    def _build_web_tab_id(self, session) -> str:
        existing = getattr(session, "web_tab_id", None)
        if existing:
            return str(existing)
        session_hash = hashlib.md5(
            f"{session.aid}|{session.device_id}|{session.web_id}".encode("utf-8")
        ).hexdigest()
        return (
            f"{session_hash[:8]}-{session_hash[8:12]}-{session_hash[12:16]}-"
            f"{session_hash[16:20]}-{session_hash[20:32]}"
        )

    async def _inject_webmssdk(self):
        assert self._page is not None
        await self._page.evaluate(
            """
            async (sdkUrl) => {
              try {
                delete window.byted_acrawler;
              } catch (e) {
                window.byted_acrawler = undefined;
              }
              const existing = document.querySelector('script[data-codex-mssdk="1"]');
              if (existing) {
                existing.remove();
              }
              await new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = sdkUrl;
                script.async = true;
                script.dataset.codexMssdk = '1';
                script.onload = () => resolve(true);
                script.onerror = () => reject(new Error('webmssdk load failed'));
                document.head.appendChild(script);
              });
              return true;
            }
            """,
            WEBMSSDK_URL,
        )


browser_runtime = BrowserRuntime()


__all__ = ["browser_runtime", "BrowserRuntime"]
