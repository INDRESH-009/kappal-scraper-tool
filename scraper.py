"""
kappal_scraper.py
─────────────────
Playwright-based async scraper for digital.kappal.co
Returns all rate cards (with full charge breakdowns) as structured JSON.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Any

from playwright.async_api import async_playwright, Page, Locator, ElementHandle

from app_paths import runtime_dir, runtime_file

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_URL      = "https://digital.kappal.co"
LOGIN_URL     = f"{BASE_URL}/login"
RATES_URL     = f"{BASE_URL}/rates"
AUTH_PROFILE_DIR = runtime_dir("kappal-auth-profile")
DEBUG_DIR = runtime_dir("debug")
PORT_QUERY_FALLBACKS = {
    "chennai": ["INMAA"],
    "new york": ["USNYC"],
    "nhava": ["INNSA"],
    "jawaharlal nehru": ["INNSA"],
    "mumbai": ["INNSA"],
}


async def authenticate_kappal(
    progress_cb: Optional[Callable[[str], Any]] = None,
    headless: bool = False,
    auth_timeout: int = 300,
) -> dict:
    """
    Opens Kappal's real login page in a persistent browser profile.
    The user enters credentials and solves CAPTCHA directly on Kappal.
    Cookies/session state are kept for later scrape runs.
    """

    async def emit(msg: str):
        if progress_cb:
            await progress_cb(msg)

    async with async_playwright() as pw:
        ctx = await _launch_context(pw, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await emit("🔐 Opening Kappal login page in a browser window ...")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
            await _wait_for_login(page, auth_timeout, emit)
            await emit("✅ Authentication saved. You can run the scraper now.")
            return {"authenticated": True, "profile_dir": str(AUTH_PROFILE_DIR)}
        finally:
            await ctx.close()


async def manual_search_and_scrape_kappal(
    progress_cb: Optional[Callable[[str], Any]] = None,
    headless: bool = False,
    search_timeout: int = 600,
) -> dict:
    """
    Opens Kappal in a real browser and lets the user perform the search manually.
    Once result cards appear, the scraper extracts all visible rate details.
    """

    async def emit(msg: str):
        if progress_cb:
            await progress_cb(msg)

    async with async_playwright() as pw:
        ctx = await _launch_context(pw, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def snap(name: str):
            try:
                path = DEBUG_DIR / f"debug_{name}.png"
                await page.screenshot(path=path, full_page=False)
                await emit(f"📸 Screenshot saved → {path}")
            except Exception:
                pass

        try:
            await emit("🌐 Opening Kappal rate search page ...")
            await page.goto(RATES_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            if "login" in page.url:
                await emit("🔐 Please log in on Kappal, solve CAPTCHA, then continue to Rate Search.")
            else:
                await emit("✅ Existing login detected.")

            await emit(
                "🧭 Fill the search form on Kappal and click Search Rates. "
                f"I will wait up to {search_timeout}s for results, then scrape automatically."
            )

            result_target = await _wait_for_results_in_context(ctx, page, search_timeout, emit)
            await snap("manual_results")

            count = await _get_result_count(result_target)
            await emit(f"📦 {count} rate card(s) visible now – scraping as more load ...")
            results = await _scrape_cards_as_they_load(
                result_target, emit, max_cards=count if count > 0 else None
            )

            return {
                "search_params": {"mode": "manual_site_search", "source_url": result_target.url},
                "total_results": len(results),
                "scraped_at": datetime.utcnow().isoformat() + "Z",
                "results": results,
            }

        except Exception as exc:
            try:
                path = DEBUG_DIR / "debug_ERROR.png"
                await page.screenshot(path=path)
                await emit(f"📸 Error screenshot → {path}")
                await emit(f"   URL at failure: {page.url}")
            except Exception:
                pass
            raise RuntimeError(f"Manual scrape failed: {exc}") from exc

        finally:
            await ctx.close()

# ─── Public entry point ───────────────────────────────────────────────────────

async def scrape_kappal(
    origin_query: str,       # e.g. "Chennai" or "INMAA"
    destination_query: str,  # e.g. "New York" or "USNYC"
    cut_off_date: str,       # e.g. "23 May 2026"
    load_type: str  = "20GP",
    quantity: int   = 1,
    origin_service_mode: str = "CY",
    destination_service_mode: str = "CY",
    origin_carrier_sd: bool = False,
    destination_carrier_sd: bool = False,
    include_nearby_origin: bool = False,
    include_nearby_destination: bool = False,
    charges: str = "Freight, Origin, Destination +1",
    search_reference_name: str = "",
    search_currency: str = "USD",
    progress_cb: Optional[Callable[[str], Any]] = None,
    headless: bool  = False,
) -> dict:
    """
    Main entry point.  Returns:
    {
      search_params: {...},
      total_results: N,
      scraped_at: ISO-string,
      results: [ {route, carrier, charges, remarks, ...}, ... ]
    }
    progress_cb(msg) is called with human-readable status strings.
    """

    async def emit(msg: str):
        if progress_cb:
            await progress_cb(msg)

    async with async_playwright() as pw:
        ctx = await _launch_context(pw, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Intercept API responses for faster/more reliable data capture
        captured_rates: list = []
        async def handle_response(response):
            if "/rates" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    if isinstance(body, dict) and "data" in body:
                        captured_rates.extend(body["data"] if isinstance(body["data"], list) else [])
                except Exception:
                    pass
        page.on("response", handle_response)

        async def snap(name: str):
            """Save a debug screenshot."""
            try:
                path = f"debug_{name}.png"
                path = DEBUG_DIR / path
                await page.screenshot(path=path, full_page=False)
                await emit(f"📸 Screenshot saved → {path}")
            except Exception:
                pass

        try:
            await emit("🔍 Navigating to rate search page …")
            await page.goto(RATES_URL, wait_until="networkidle")
            await asyncio.sleep(1)
            if "login" in page.url:
                raise RuntimeError(
                    "Not authenticated yet. Click Authenticate first, log in on Kappal, "
                    "then run the scraper again."
                )

            await snap("02_rates_page")
            await emit(f"   URL: {page.url}")

            await emit("🔍 Filling search form …")
            await _fill_search_form(page, origin_query, destination_query,
                                    cut_off_date, load_type, quantity,
                                    origin_service_mode, destination_service_mode,
                                    origin_carrier_sd, destination_carrier_sd,
                                    include_nearby_origin, include_nearby_destination,
                                    charges, search_reference_name, search_currency,
                                    emit=emit, snap=snap)

            await emit("⏳ Waiting for results …")
            await _wait_for_results(page)
            await snap("05_results")

            count = await _get_result_count(page)
            await emit(f"📦 {count} rate(s) found – opening each card …")

            results = await _scrape_all_cards(page, count, emit)

            return {
                "search_params": {
                    "origin":        origin_query,
                    "destination":   destination_query,
                    "cut_off_date":  cut_off_date,
                    "load_type":     load_type,
                    "quantity":      quantity,
                    "origin_service_mode": origin_service_mode,
                    "destination_service_mode": destination_service_mode,
                    "origin_carrier_sd": origin_carrier_sd,
                    "destination_carrier_sd": destination_carrier_sd,
                    "include_nearby_origin": include_nearby_origin,
                    "include_nearby_destination": include_nearby_destination,
                    "charges": charges,
                    "search_reference_name": search_reference_name,
                    "search_currency": search_currency,
                },
                "total_results": len(results),
                "scraped_at":    datetime.utcnow().isoformat() + "Z",
                "results":       results,
            }

        except Exception as exc:
            try:
                path = DEBUG_DIR / "debug_ERROR.png"
                await page.screenshot(path=path)
                await emit(f"📸 Error screenshot → {path}  (open it to see what went wrong)")
                await emit(f"   URL at failure: {page.url}")
            except Exception:
                pass
            raise RuntimeError(f"Scraping failed: {exc}") from exc

        finally:
            await ctx.close()


# ─── Login ────────────────────────────────────────────────────────────────────

async def _launch_context(pw, headless: bool = False):
    return await pw.chromium.launch_persistent_context(
        str(AUTH_PROFILE_DIR),
        headless=headless,
        slow_mo=80 if not headless else 0,
        viewport={"width": 1440, "height": 900},
    )


async def _wait_for_login(page: Page, auth_timeout: int = 300, emit=None):
    if emit:
        await emit(
            f"⏸  Enter your credentials on Kappal, solve CAPTCHA, and click Login. "
            f"Waiting up to {auth_timeout}s ..."
        )

    deadline = asyncio.get_event_loop().time() + auth_timeout
    while asyncio.get_event_loop().time() < deadline:
        if "login" not in page.url:
            break
        await asyncio.sleep(1)
    else:
        raise RuntimeError(
            f"Authentication was not completed within {auth_timeout} seconds. "
            "Please try again or increase the auth timeout."
        )

    if emit:
        await emit("✅ Login detected.")

    await page.wait_for_load_state("networkidle")


# ─── Search form ──────────────────────────────────────────────────────────────

async def _fill_search_form(
    page: Page, origin: str, destination: str,
    cut_off_date: str, load_type: str, quantity: int,
    origin_service_mode: str, destination_service_mode: str,
    origin_carrier_sd: bool, destination_carrier_sd: bool,
    include_nearby_origin: bool, include_nearby_destination: bool,
    charges: str, search_reference_name: str, search_currency: str,
    emit=None, snap=None,
):
    async def log(msg):
        if emit: await emit(msg)

    # ── Save full form HTML for diagnosis ────────────────────────────────────
    try:
        form_html = await page.evaluate("""
            () => {
                // Find the main form/search area
                const containers = ['form', '[class*="search"]', '[class*="rate"]', 'main', 'body'];
                for (const sel of containers) {
                    const el = document.querySelector(sel);
                    if (el) return el.innerHTML.slice(0, 8000);
                }
                return document.body.innerHTML.slice(0, 8000);
            }
        """)
        debug_html = runtime_file("debug_form_html.txt")
        with debug_html.open("w", encoding="utf-8") as f:
            f.write(form_html)
        await log(f"📄 Form HTML saved → {debug_html} (open to inspect DOM)")
    except Exception as e:
        await log(f"   Could not save HTML: {e}")

    # ── Dump all clickable elements that look like port fields ────────────────
    clickables = await page.evaluate("""
        () => {
            const all = document.querySelectorAll('div, span, input');
            return Array.from(all)
                .filter(el => {
                    const t = el.innerText || el.value || el.placeholder || '';
                    const visible = el.offsetParent !== null && el.offsetWidth > 50;
                    return visible && (
                        t.toLowerCase().includes('origin') ||
                        t.toLowerCase().includes('destination') ||
                        t.toLowerCase().includes('port') ||
                        t.toLowerCase().includes('nhava') ||
                        t.toLowerCase().includes('chennai') ||
                        t.toLowerCase().includes('new york') ||
                        el.tagName === 'INPUT'
                    );
                })
                .slice(0, 30)
                .map(el => ({
                    tag: el.tagName,
                    text: (el.innerText || el.value || el.placeholder || '').slice(0,60),
                    className: el.className.slice(0,80),
                    id: el.id,
                }));
        }
    """)
    await log("🔬 Clickable port-like elements found:")
    for c in clickables:
        await log(f"   <{c['tag'].lower()} class='{c['className'][:60]}' id='{c['id']}'> '{c['text']}'")

    # ── Origin ────────────────────────────────────────────────────────────────
    await _set_service_mode(page, "origin", origin_service_mode, emit)
    await _set_section_checkbox(page, "origin", "Carrier SD Services", origin_carrier_sd, emit)
    await _set_section_checkbox(page, "origin", "Include Nearby", include_nearby_origin, emit)
    await log(f"✏️  Filling origin: '{origin}' …")
    if not await _kappal_port_fill(page, section="origin", query=origin, emit=emit):
        raise RuntimeError(
            f"Origin port '{origin}' was not found. Try entering the exact Kappal port code, "
            "for example INMAA or INNSA."
        )
    if snap: await snap("03a_origin_filled")
    await asyncio.sleep(0.5)

    # ── Destination ───────────────────────────────────────────────────────────
    await _set_service_mode(page, "destination", destination_service_mode, emit)
    await _set_section_checkbox(page, "destination", "Carrier SD Services", destination_carrier_sd, emit)
    await _set_section_checkbox(page, "destination", "Include Nearby", include_nearby_destination, emit)
    await log(f"✏️  Filling destination: '{destination}' …")
    if not await _kappal_port_fill(page, section="destination", query=destination, emit=emit):
        raise RuntimeError(
            f"Destination port '{destination}' was not found. Try entering the exact Kappal port code, "
            "for example USNYC."
        )
    if snap: await snap("03b_dest_filled")
    await asyncio.sleep(0.5)

    # ── Date ─────────────────────────────────────────────────────────────────
    await log(f"📅  Setting date: '{cut_off_date}' …")
    await _fill_date(page, cut_off_date, emit)
    if snap: await snap("03c_date_filled")

    # ── Load type ─────────────────────────────────────────────────────────────
    await log(f"📦  Setting load type: '{load_type} x{quantity}' …")
    await _fill_load_type(page, load_type, quantity, emit)
    if snap: await snap("03d_loadtype_filled")
    await asyncio.sleep(0.3)

    await _fill_charges(page, charges, emit)
    await _fill_reference_name(page, search_reference_name, emit)
    await _fill_currency(page, search_currency, emit)

    # ── Dump all visible buttons before clicking ──────────────────────────────
    btns_info = await page.evaluate("""
        () => Array.from(document.querySelectorAll('button'))
            .filter(b => b.offsetParent !== null)
            .map(b => ({text: b.innerText.trim().slice(0,40), cls: b.className.slice(0,60)}))
    """)
    await log(f"🔎  Visible buttons: {[b['text'] for b in btns_info]}")

    # ── Click Search Rates ────────────────────────────────────────────────────
    clicked = False
    for btn_text in ["Search Rates", "Search", "Find Rates", "Go"]:
        btn = page.locator(f"button:has-text('{btn_text}')").last
        if await btn.count() > 0 and await btn.is_visible():
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await log(f"   ✅ Clicked button: '{btn_text}'")
            clicked = True
            break

    if not clicked:
        await log("   ⚠️  Search button not found by text — trying submit type")
        submit = page.locator("button[type='submit'], input[type='submit']").last
        if await submit.count() > 0:
            await submit.click()
            clicked = True

    if not clicked:
        await log("   ❌ Could not find Search Rates button — check debug_form_html.txt")

    await asyncio.sleep(2)
    await page.wait_for_load_state("networkidle")
    await log(f"   URL after search click: {page.url}")
    if snap: await snap("04_after_search_click")


async def _kappal_port_fill(page: Page, section: str, query: str, emit=None) -> bool:
    """
    Fills the Origin or Destination port autocomplete on Kappal.

    Kappal renders port fields as styled containers (div/span with anchor icon).
    Clicking the container opens a search overlay with an actual <input> inside.
    This function:
      1. Finds and clicks the correct container (origin or destination)
      2. Waits for the search input to appear in the overlay
      3. Types the query and clicks the first suggestion
    """
    async def log(m):
        if emit: await emit(m)

    # Prefer the real Angular Material autocomplete inputs. Broad container
    # clicks can leave the suggestions panel intercepting the next click.
    direct_selectors = (
        [
            "input[name='fcl_origin_port']:visible",
            "input#originLocation:visible",
            "md-input-container[md-input-id='originLocation'] input:visible",
            "input[aria-owns='ul-6']:visible",
        ]
        if section == "origin"
        else [
            "input[name='fcl_destination_port']:visible",
            "input#destinationLocation:visible",
            "md-input-container[md-input-id='destinationLocation'] input:visible",
            "input[id*='destination' i]:visible",
        ]
    )

    for sel in direct_selectors:
        inp = page.locator(sel).first
        if await inp.count() > 0 and await inp.is_visible():
            await log(f"   Found {section} input via: {sel}")
            return await _type_port_and_pick(page, inp, query, emit)

    # Step 1: Click the right container to open the search overlay.
    # We use broad selectors and pick the one matching origin or destination
    # context only when direct input lookup fails.

    container_clicked = False

    # Strategy A: find container by aria-label or data attributes
    for attr_sel in [
        f"[aria-label*='{section}' i]",
        f"[data-field*='{section}' i]",
        f"[placeholder*='{section}' i]",
        f"[class*='{section}']:visible",
    ]:
        el = page.locator(attr_sel).first
        if await el.count() > 0 and await el.is_visible():
            await el.click(force=True)
            await log(f"   Clicked container via: {attr_sel}")
            container_clicked = True
            break

    # Strategy B: The form has two identical-looking port selector areas.
    # Origin is on the LEFT side (first), Destination on the RIGHT (second).
    # Find all anchor-icon port containers and pick index 0 or 1.
    if not container_clicked:
        # Look for the styled field that shows port names (has anchor ⚓ icon next to it)
        port_containers = page.locator(
            # Common patterns for these custom port picker components
            "[class*='port']:visible, "
            "[class*='Port']:visible, "
            "[class*='location']:visible, "
            "[class*='Location']:visible, "
            "[class*='search-field']:visible, "
            # Containers that have an svg/icon + text pattern
            "div:has(svg):has-text('INNSA'):visible, "
            "div:has(svg):has-text('USNYC'):visible, "
            "div:has(svg):has-text('Nhava'):visible, "
            "div:has(svg):has-text('New York'):visible"
        )
        idx = 0 if section == "origin" else 1
        c = await port_containers.count()
        await log(f"   Found {c} port containers for section='{section}' (want index {idx})")
        if c > idx:
            await port_containers.nth(idx).click(force=True)
            container_clicked = True
            await log(f"   Clicked port container #{idx}")

    # Strategy C: two halves of the form — click left or right side
    if not container_clicked:
        await log(f"   Trying positional click strategy for '{section}' …")
        # Evaluate JS to find and click the container by position
        result = await page.evaluate(f"""
            (section) => {{
                // Find all divs/spans that have the anchor SVG icon pattern
                // Kappal port fields typically have class containing 'input' or 'field'
                const candidates = Array.from(document.querySelectorAll(
                    'div, span, input'
                )).filter(el => {{
                    const rect = el.getBoundingClientRect();
                    const visible = rect.width > 100 && rect.height > 20 && rect.top > 0;
                    const hasPortText = el.innerText && (
                        el.innerText.includes('INNSA') ||
                        el.innerText.includes('USNYC') ||
                        el.innerText.includes('INMAA') ||
                        el.innerText.includes('Nhava') ||
                        el.innerText.includes('Chennai') ||
                        el.innerText.includes('New York') ||
                        el.innerText.includes('Sheva')
                    );
                    return visible && hasPortText && rect.width < 700;
                }});

                if (candidates.length === 0) return {{found: false, count: 0}};

                // Sort by left position — origin is leftmost
                candidates.sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                const idx = section === 'origin' ? 0 : candidates.length - 1;
                const el = candidates[idx];
                const rect = el.getBoundingClientRect();
                el.click();
                return {{
                    found: true,
                    count: candidates.length,
                    clicked: el.tagName + ' ' + el.className.slice(0,60),
                    text: (el.innerText || '').slice(0, 50),
                    left: rect.left,
                }};
            }}
        """, section)
        await log(f"   JS click result: {result}")
        if result.get('found'):
            container_clicked = True

    if not container_clicked:
        await log(f"   ⚠️  Could not find {section} container. Check debug_form_html.txt")
        return False

    # Step 2: Wait for overlay / search input to appear
    await asyncio.sleep(0.6)

    overlay_input = None
    overlay_selectors = [
        # Common overlay/modal input patterns
        "[role='dialog'] input:visible",
        "[role='combobox']:visible",
        "[class*='overlay'] input:visible",
        "[class*='modal'] input:visible",
        "[class*='popup'] input:visible",
        "[class*='dropdown'] input:visible",
        "[class*='search'] input:visible",
        # Generic: any newly visible input with relevant placeholder
        "input[placeholder*='search' i]:visible",
        "input[placeholder*='port' i]:visible",
        "input[placeholder*='type' i]:visible",
        "input[autofocus]:visible",
    ]

    for sel in overlay_selectors:
        inp = page.locator(sel)
        if await inp.count() > 0:
            if await inp.first.is_visible():
                overlay_input = inp.first
                await log(f"   Found overlay input via: {sel}")
                break

    # Fallback: any focused input that appeared after the click
    if overlay_input is None:
        await log("   Trying any focused / newly visible input …")
        all_visible = page.locator("input:visible")
        c = await all_visible.count()
        await log(f"   Total visible inputs after click: {c}")
        for i in range(c):
            inp = all_visible.nth(i)
            ph = await inp.get_attribute("placeholder") or ""
            cls = await inp.get_attribute("class") or ""
            await log(f"      input[{i}] placeholder='{ph}' class='{cls[:60]}'")
        if c > 0:
            overlay_input = all_visible.first
            await log(f"   Falling back to first visible input")

    if overlay_input is None:
        await log(f"   ❌ No input appeared after clicking {section} container")
        return False

    return await _type_port_and_pick(page, overlay_input, query, emit)


async def _type_port_and_pick(page: Page, input_locator: Locator, query: str, emit=None) -> bool:
    async def log(m):
        if emit: await emit(m)

    queries = [query]
    normalized = query.strip().lower()
    queries.extend(PORT_QUERY_FALLBACKS.get(normalized, []))

    suggestion_selectors = [
        "[role='option']:visible",
        "md-autocomplete-parent-scope:visible",
        "md-virtual-repeat-container li:visible",
        "[class*='suggestion']:visible",
        "[class*='option']:visible",
        "[class*='result']:visible",
        "li:visible",
        "[role='listbox'] div:visible",
    ]

    for attempt in queries:
        # Fill without pointer-clicking the input; Kappal's md-autocomplete panel can
        # cover the field and intercept pointer events.
        await input_locator.focus()
        await input_locator.fill("")
        await input_locator.type(attempt, delay=80)
        await _wait_for_app_idle(page, timeout=12_000)
        await asyncio.sleep(0.8)

        for sel in suggestion_selectors:
            suggestions = page.locator(sel)
            c = await suggestions.count()
            if c == 0:
                continue

            chosen = None
            not_found_seen = False
            for i in range(min(c, 8)):
                candidate = suggestions.nth(i)
                text = (await candidate.inner_text()).strip()
                if not text:
                    continue
                if "no ports matching" in text.lower() or "no results" in text.lower():
                    not_found_seen = True
                    continue
                if attempt.lower() in text.lower() or query.lower() in text.lower():
                    chosen = candidate
                    break
                if chosen is None:
                    chosen = candidate

            if chosen is not None and await chosen.is_visible():
                text = (await chosen.inner_text()).strip()
                await log(f"   ✅ Selecting port suggestion: '{text[:60]}' (via {sel})")
                await chosen.click(force=True)
                await _wait_for_app_idle(page, timeout=12_000)
                await asyncio.sleep(0.4)
                return True

            if not_found_seen:
                await log(f"   ⚠️  No port match for '{attempt}'")
                break

    await page.keyboard.press("Escape")
    await log(f"   ❌ Kappal did not return a selectable port for '{query}'")
    return False


async def _set_service_mode(page: Page, section: str, mode: str, emit=None):
    async def log(m):
        if emit: await emit(m)

    mode = (mode or "CY").upper()
    if mode not in {"CY", "DOOR"}:
        await log(f"   ⚠️  Unsupported {section} service mode '{mode}', keeping current")
        return

    changed = await page.evaluate(
        """
        ({section, mode}) => {
            const blocks = Array.from(document.querySelectorAll('.mobile_input, .search_multiport_div'));
            const block = blocks.find(el => (el.innerText || '').toLowerCase().includes(section));
            if (!block) return {found: false};
            const buttons = Array.from(block.querySelectorAll('button'))
                .filter(btn => (btn.innerText || '').trim().toUpperCase() === mode);
            if (!buttons.length) return {found: false};
            const button = buttons[0];
            const already = button.className.includes('search-tab-active');
            if (!already) button.click();
            return {found: true, already};
        }
        """,
        {"section": section, "mode": mode},
    )
    if changed.get("found"):
        await log(f"   ✅ {section.title()} mode set to {mode}")
        await asyncio.sleep(0.3)


async def _wait_for_app_idle(page: Page, timeout: int = 20_000):
    try:
        await page.wait_for_function(
            """
            () => {
                const progress = document.querySelector('#toolbar-progress');
                const spinner = document.querySelector('.loader-spin');
                const visible = el => !!el && el.offsetParent !== null;
                return !visible(progress) && !visible(spinner);
            }
            """,
            timeout=timeout,
        )
    except Exception:
        pass


async def _set_section_checkbox(page: Page, section: str, label: str, should_check: bool, emit=None):
    async def log(m):
        if emit: await emit(m)

    result = await page.evaluate(
        """
        ({section, label, shouldCheck}) => {
            const blocks = Array.from(document.querySelectorAll('.mobile_input, .search_multiport_div'));
            const block = blocks.find(el => (el.innerText || '').toLowerCase().includes(section));
            if (!block) return {found: false};
            const boxes = Array.from(block.querySelectorAll('md-checkbox, [role="checkbox"]'));
            const box = boxes.find(el => (el.innerText || '').toLowerCase().includes(label.toLowerCase()));
            if (!box) return {found: false};
            const checked = box.getAttribute('aria-checked') === 'true';
            if (checked !== shouldCheck) box.click();
            return {found: true, checked};
        }
        """,
        {"section": section, "label": label, "shouldCheck": should_check},
    )
    if result.get("found"):
        await log(f"   ✅ {section.title()} {label}: {'on' if should_check else 'off'}")
        await asyncio.sleep(0.2)


async def _fill_date(page: Page, date_str: str, emit=None):
    """Fills the cut-off date field."""
    async def log(m):
        if emit: await emit(m)

    date_selectors = [
        ".md-datepicker-input:visible",
        "input.md-datepicker-input:visible",
        "input[placeholder*='date' i]",
        "input[placeholder*='cut' i]",
        "input[name*='date' i]",
        "input[name*='cut' i]",
        "input[type='date']",
    ]

    for sel in date_selectors:
        el = page.locator(sel)
        if await el.count() > 0:
            visible = await el.first.is_visible()
            if visible:
                await el.first.click()
                await asyncio.sleep(0.3)
                await el.first.triple_click()
                await el.first.fill(_kappal_date_value(date_str))
                await page.keyboard.press("Tab")
                await log(f"   ✅ Date set via {sel}")
                return

    # Fallback: look for a calendar/date icon and click it
    await log("   ⚠️  Date input not found by selector — trying calendar icon …")
    icon = page.locator(
        "svg[class*='calendar' i], [class*='calendar-icon'], [class*='datepicker'] svg"
    ).first
    if await icon.count() > 0:
        await icon.click()
        await asyncio.sleep(0.5)
        # After opening calendar, try to find a text input that appeared
        inp = page.locator("input[class*='date']:visible, .react-datepicker__input-container input:visible")
        if await inp.count() > 0:
            await inp.first.triple_click()
            await inp.first.fill(_kappal_date_value(date_str))
            await page.keyboard.press("Enter")
            await log("   ✅ Date set via calendar icon fallback")
            return

    await log("   ⚠️  Could not fill date field — may need manual selector")


def _kappal_date_value(date_str: str) -> str:
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return date_str


async def _fill_load_type(page: Page, load_type: str, quantity: int, emit=None):
    """Fills the load type / container type dropdown."""
    async def log(m):
        if emit: await emit(m)

    load_label = f"{load_type} x{quantity}"

    # Try native <select> first
    for sel in [
        "select[name*='load' i]", "select[name*='container' i]",
        "select[name*='equipment' i]", "select",
    ]:
        el = page.locator(sel)
        if await el.count() > 0 and await el.first.is_visible():
            try:
                await el.first.select_option(label=load_label)
                await log(f"   ✅ Load type set via <select>")
                return
            except Exception:
                try:
                    await el.first.select_option(label=load_type)
                    await log(f"   ✅ Load type set via <select>")
                    return
                except Exception:
                    pass

    # Custom dropdown: look for a visible element already showing a container type
    current_selectors = [
        "#search-open-form .panel-select:has-text('GP'):visible",
        "#search-open-form md-select:has-text('GP'):visible",
        "#search-open-form button:has-text('GP'):visible",
        "#search-open-form div:has-text('20GP'):visible",
        "#search-open-form div:has-text('40GP'):visible",
        "#search-open-form div:has-text('40HC'):visible",
    ]
    for sel in current_selectors:
        await _wait_for_app_idle(page)
        trigger = page.locator(sel).first
        if await trigger.count() > 0 and await trigger.is_visible():
            await trigger.click(force=True)
            await asyncio.sleep(0.4)
            # Find the option in the opened menu
            option = page.locator(
                f"[role='option']:has-text('{load_label}'),"
                f"li:has-text('{load_label}'),"
                f"div[class*='option']:has-text('{load_label}'),"
                f"[role='option']:has-text('{load_type}'),"
                f"li:has-text('{load_type}'),"
                f"div[class*='option']:has-text('{load_type}')"
            ).first
            if await option.count() > 0 and await option.is_visible():
                await option.click(force=True)
                await _wait_for_app_idle(page)
                await log(f"   ✅ Load type '{load_label}' selected from custom dropdown")
                return
            await page.keyboard.press("Escape")

    result = await page.evaluate(
        """
        ({loadType, loadLabel}) => {
            const root = document.querySelector('#search-open-form') || document.body;
            const candidates = Array.from(root.querySelectorAll('div, md-select, button'))
                .filter(el => {
                    const text = el.innerText || '';
                    const rect = el.getBoundingClientRect();
                    return rect.width > 80 && rect.height > 20 && /\\b(20GP|40GP|40HC)\\b/.test(text);
                });
            const trigger = candidates.find(el => (el.innerText || '').includes(loadType)) || candidates[0];
            if (!trigger) return {found: false};
            trigger.click();
            return {found: true, text: (trigger.innerText || '').slice(0, 80)};
        }
        """,
        {"loadType": load_type, "loadLabel": load_label},
    )
    if result.get("found"):
        await asyncio.sleep(0.4)
        option = page.locator(
            f"[role='option']:has-text('{load_label}'),"
            f"md-option:has-text('{load_label}'),"
            f"li:has-text('{load_label}'),"
            f"[role='option']:has-text('{load_type}'),"
            f"md-option:has-text('{load_type}'),"
            f"li:has-text('{load_type}')"
        ).first
        if await option.count() > 0 and await option.is_visible():
            await option.click(force=True)
            await _wait_for_app_idle(page)
            await log(f"   ✅ Load type '{load_label}' selected from fallback dropdown")
            return
        await page.keyboard.press("Escape")

    await log(f"   ⚠️  Could not set load type — may need manual selector")


async def _fill_charges(page: Page, charges: str, emit=None):
    async def log(m):
        if emit: await emit(m)

    if not charges:
        return

    trigger = page.locator(
        ".panel-select-charge:visible, "
        "div:has-text('Locals & Custom Charges'):visible, "
        "div:has-text('Freight, Origin'):visible"
    ).last
    if await trigger.count() == 0 or not await trigger.is_visible():
        return

    current = (await trigger.inner_text()).strip()
    if charges.lower() in current.lower() or current.lower() in charges.lower():
        await log(f"   ✅ Charges already set: {current[:60]}")
        return

    await trigger.click(force=True)
    await asyncio.sleep(0.4)
    option = page.locator(
        f"[role='option']:has-text('{charges}'), "
        f"li:has-text('{charges}'), "
        f"md-option:has-text('{charges}'), "
        f"div[class*='option']:has-text('{charges}')"
    ).first
    if await option.count() > 0 and await option.is_visible():
        await option.click(force=True)
        await log(f"   ✅ Charges set to {charges}")
    else:
        await page.keyboard.press("Escape")
        await log("   ⚠️  Could not change charges; keeping current selection")


async def _fill_reference_name(page: Page, reference_name: str, emit=None):
    async def log(m):
        if emit: await emit(m)

    if not reference_name:
        return

    ref = page.locator(
        "input[placeholder*='reference' i]:visible, "
        "input[name*='reference' i]:visible"
    ).first
    if await ref.count() > 0 and await ref.is_visible():
        await ref.fill(reference_name)
        await page.keyboard.press("Tab")
        await log("   ✅ Search reference name set")


async def _fill_currency(page: Page, currency: str, emit=None):
    async def log(m):
        if emit: await emit(m)

    currency = (currency or "USD").upper()
    trigger = page.locator(
        "select[name*='currency' i]:visible, "
        "md-select:has-text('USD'):visible, "
        "div:has-text('Search Currency'):visible, "
        "div:has-text('USD'):visible"
    ).last
    if await trigger.count() == 0 or not await trigger.is_visible():
        return

    tag_name = (await trigger.evaluate("el => el.tagName")).lower()
    if tag_name == "select":
        try:
            await trigger.select_option(label=currency)
            await log(f"   ✅ Currency set to {currency}")
        except Exception:
            pass
        return

    current = (await trigger.inner_text()).strip()
    if currency in current:
        await log(f"   ✅ Currency already set to {currency}")
        return

    await trigger.click(force=True)
    await asyncio.sleep(0.4)
    option = page.locator(
        f"[role='option']:has-text('{currency}'), "
        f"md-option:has-text('{currency}'), "
        f"li:has-text('{currency}')"
    ).first
    if await option.count() > 0 and await option.is_visible():
        await option.click(force=True)
        await log(f"   ✅ Currency set to {currency}")
    else:
        await page.keyboard.press("Escape")


# ─── Results ──────────────────────────────────────────────────────────────────

async def _wait_for_results(page: Page, timeout: int = 25_000):
    """Waits until at least one result card is visible."""
    await page.wait_for_function(
        """
        () => {
            const text = document.body?.innerText || '';
            const urlLooksLikeResults = /\\/rates\\/(fcl|lcl|air|land)\\//i.test(location.pathname);
            const hasCount = /\\d+\\s+rates?\\s+found/i.test(text);
            const hasFoundCount = /found\\s+\\d+\\s+rates?/i.test(text);
            const hasInstantRates = /Instant Rates/i.test(text);
            const hasFetching = /Fetching\\s+in\\s+progress/i.test(text);
            const hasDetailsButton = Array.from(document.querySelectorAll('button, a, div, span'))
                .some(el => /^View\\s+Details$/i.test((el.innerText || '').trim()));
            const hasRateCards = !!document.querySelector(
                '[class*="rate-card"], [class*="rateCard"], [class*="result-card"], [class*="instant-rate"]'
            );
            return hasDetailsButton || (urlLooksLikeResults && (hasCount || hasFoundCount || hasInstantRates || hasFetching || hasRateCards));
        }
        """,
        timeout=timeout,
    )
    await asyncio.sleep(0.5)


async def scrape_current_results_page(
    page: Page,
    emit,
    quiet_seconds: int = 20,
    max_seconds: int = 240,
) -> list:
    """
    Scrapes a Kappal results page that has already been reached by another flow.
    Batch automation should fill/search; this helper owns result-card scraping.
    """
    if not _is_results_url(page.url):
        await _wait_for_results(page, timeout=120_000)
    else:
        await asyncio.sleep(1)
    count = await _get_result_count(page)
    await emit(f"📦 Results page detected at {page.url}; count={count}.")
    return await _scrape_cards_as_they_load(
        page,
        emit,
        quiet_seconds=quiet_seconds,
        max_seconds=max_seconds,
    )


async def _wait_for_results_in_context(ctx, fallback_page: Page, timeout_seconds: int, emit=None):
    async def log(msg: str):
        if emit:
            await emit(msg)

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_status_at = 0
    last_urls = []
    result_url_seen_at = {}

    while asyncio.get_event_loop().time() < deadline:
        pages = list(ctx.pages) or [fallback_page]
        last_urls = []
        now = asyncio.get_event_loop().time()

        for candidate in _result_targets(pages):
            try:
                last_urls.append(candidate.url)
                if await _page_has_results(candidate):
                    diag = await _result_page_diagnostics(candidate)
                    await log(f"✅ Results detected in {candidate.url}. {diag}")
                    await asyncio.sleep(0.8)
                    return candidate
                if _is_results_url(candidate.url):
                    result_url_seen_at.setdefault(candidate, now)
                    elapsed = now - result_url_seen_at[candidate]
                    if elapsed >= 15:
                        candidate = await _best_result_target(candidate)
                        diag = await _result_page_diagnostics(candidate)
                        await log(
                            f"✅ Result URL has been stable for {int(elapsed)}s; "
                            f"starting scrape. {diag}"
                        )
                        return candidate
            except Exception:
                continue

        if now - last_status_at >= 15:
            last_status_at = now
            urls = " | ".join(last_urls[-3:]) if last_urls else "no open Kappal pages detected"
            await log(f"⏳ Still waiting for result cards. Watching: {urls}")

        await asyncio.sleep(2)

    urls = " | ".join(last_urls[-5:]) if last_urls else "no pages"
    raise RuntimeError(
        f"Timed out waiting for Kappal results after {timeout_seconds}s. "
        f"Last observed page(s): {urls}"
    )


def _is_results_url(url: str) -> bool:
    return bool(re.search(r"/rates/(fcl|lcl|air|land)/", url or "", re.I))


def _result_targets(pages: list):
    targets = []
    for page in pages:
        targets.append(page)
        try:
            targets.extend(page.frames)
        except Exception:
            pass
    seen = set()
    unique = []
    for target in targets:
        ident = id(target)
        if ident not in seen:
            seen.add(ident)
            unique.append(target)
    return unique


async def _best_result_target(target):
    candidates = [target]
    try:
        candidates.extend(target.frames)
    except Exception:
        pass

    best = target
    best_score = -1
    for candidate in candidates:
        try:
            result = await _result_detection(candidate)
            score = (
                (100 if result.get("hasDetailsButton") else 0)
                + (50 if result.get("hasCount") else 0)
                + (50 if result.get("hasFoundCount") else 0)
                + (20 if result.get("hasRateText") else 0)
                + min(int(result.get("textLength") or 0), 10000) / 10000
            )
            if score > best_score:
                best = candidate
                best_score = score
        except Exception:
            pass
    return best


async def _result_detection(target) -> dict:
    return await target.evaluate(
        """
        () => {
            const deepText = (root) => {
                let out = '';
                const visit = (node) => {
                    if (!node) return;
                    if (node.nodeType === Node.TEXT_NODE) out += ' ' + node.textContent;
                    if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_NODE) return;
                    const el = node;
                    if (el.shadowRoot) visit(el.shadowRoot);
                    for (const child of el.childNodes || []) visit(child);
                };
                visit(root);
                return out.replace(/\\s+/g, ' ').trim();
            };
            const deepElements = (root) => {
                const out = [];
                const visit = (node) => {
                    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                    out.push(node);
                    if (node.shadowRoot) {
                        for (const child of node.shadowRoot.children || []) visit(child);
                    }
                    for (const child of node.children || []) visit(child);
                };
                for (const child of root.children || []) visit(child);
                return out;
            };
            const text = deepText(document);
            const path = location.pathname || '';
            const urlLooksLikeResults = /\\/rates\\/(fcl|lcl|air|land)\\//i.test(path);
            const hasCount = /\\d+\\s+rates?\\s+found/i.test(text);
            const hasFoundCount = /found\\s+\\d+\\s+rates?/i.test(text);
            const hasInstantRates = /Instant Rates/i.test(text);
            const hasFetching = /Fetching\\s+in\\s+progress/i.test(text);
            const hasDetailsButton = deepElements(document.documentElement)
                .some(el => /^View\\s+Details$/i.test((el.innerText || '').trim()));
            const hasRateText = /Freight Rate/i.test(text) && /Total Rate/i.test(text);
            return {
                ok: hasDetailsButton
                    || (urlLooksLikeResults && (hasCount || hasFoundCount || hasInstantRates || hasFetching))
                    || (hasInstantRates && (hasCount || hasFoundCount))
                    || hasRateText,
                textLength: text.length,
                hasCount,
                hasFoundCount,
                hasDetailsButton,
                hasRateText,
            };
        }
        """
    )


async def _page_has_results(target) -> bool:
    result = await _result_detection(target)
    return bool(result.get("ok")) if isinstance(result, dict) else bool(result)


async def _result_page_diagnostics(page: Page) -> str:
    try:
        data = await page.evaluate(
            """
            () => {
                const deepText = (root) => {
                    let out = '';
                    const visit = (node) => {
                        if (!node) return;
                        if (node.nodeType === Node.TEXT_NODE) out += ' ' + node.textContent;
                        if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_NODE) return;
                        if (node.shadowRoot) visit(node.shadowRoot);
                        for (const child of node.childNodes || []) visit(child);
                    };
                    visit(root);
                    return out.replace(/\\s+/g, ' ').trim();
                };
                const deepElements = (root) => {
                    const out = [];
                    const visit = (node) => {
                        if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                        out.push(node);
                        if (node.shadowRoot) for (const child of node.shadowRoot.children || []) visit(child);
                        for (const child of node.children || []) visit(child);
                    };
                    for (const child of root.children || []) visit(child);
                    return out;
                };
                const text = deepText(document);
                const buttons = deepElements(document.documentElement)
                    .filter(el => ['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role') === 'button')
                    .map(el => (el.innerText || el.textContent || '').trim())
                    .filter(Boolean)
                    .slice(0, 12);
                const countMatch = text.match(/\\d+\\s+rates?\\s+found/i) || text.match(/found\\s+\\d+\\s+rates?/i);
                return {
                    textLength: text.length,
                    countText: countMatch ? countMatch[0] : null,
                    buttons,
                    sample: text.slice(0, 140).replace(/\\s+/g, ' '),
                };
            }
            """
        )
        return (
            f"DOM text={data.get('textLength')}, "
            f"count={data.get('countText')}, "
            f"buttons={data.get('buttons')}, "
            f"sample='{data.get('sample')}'"
        )
    except Exception as exc:
        return f"Could not read page diagnostics: {exc}"


async def _get_result_count(page: Page) -> int:
    """Reads the 'N rates found' headline."""
    try:
        el = page.locator("text=/\\d+\\s+rates?\\s+found/i")
        if await el.count() > 0:
            m = re.search(r"(\d+)", await el.first.inner_text())
            return int(m.group(1)) if m else 0
    except Exception:
        pass
    try:
        count = await page.evaluate(
            """
            () => {
                const deepText = (root) => {
                    let out = '';
                    const visit = (node) => {
                        if (!node) return;
                        if (node.nodeType === Node.TEXT_NODE) out += ' ' + node.textContent;
                        if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_NODE) return;
                        if (node.shadowRoot) visit(node.shadowRoot);
                        for (const child of node.childNodes || []) visit(child);
                    };
                    visit(root);
                    return out.replace(/\\s+/g, ' ').trim();
                };
                const text = deepText(document);
                const m = text.match(/(\\d+)\\s+rates?\\s+found/i) || text.match(/found\\s+(\\d+)\\s+rates?/i);
                return m ? Number(m[1]) : 0;
            }
            """
        )
        if count:
            return int(count)
    except Exception:
        pass
    # Fallback: count View Details buttons
    locator_count = await _view_details_buttons(page).count()
    if locator_count:
        return locator_count
    text_count = await _text_view_details_count(page)
    if text_count:
        return text_count
    return await _deep_view_details_count(page)


async def _scrape_all_cards(page: Page, total: int, emit) -> list:
    results = []
    for i in range(total):
        await emit(f"  ↳ Card {i + 1} / {total} …")
        try:
            # Re-query every iteration (React may re-render the list)
            btns = _view_details_buttons(page)
            if await btns.count() > i:
                btn = btns.nth(i)
            else:
                btn = None

            # Extract summary from the card container BEFORE opening modal
            summary = {}
            if btn is not None:
                card_box = btn.locator(
                    "xpath=ancestor::div[contains(@class,'card') or contains(@class,'Card') "
                    "or contains(@class,'rate') or contains(@class,'Rate')][1]"
                )
                summary = await _extract_card_summary(card_box)

            # Open modal
            if btn is not None:
                await btn.scroll_into_view_if_needed()
                await btn.click()
            else:
                await _deep_click_view_details(page, i)
            await page.wait_for_selector(
                "[role='dialog'], .modal, [class*='Modal'], [class*='details-modal']",
                timeout=10_000,
            )
            await asyncio.sleep(0.6)

            # Scrape modal
            details = await _extract_modal(page)

            results.append({**summary, **details})

        except Exception as exc:
            results.append({"_error": str(exc), "_card_index": i})

        finally:
            await _close_modal(page)
            await asyncio.sleep(0.5)

    return results


async def _scrape_cards_as_they_load(
    page: Page,
    emit,
    quiet_seconds: int = 45,
    max_seconds: int = 900,
    max_cards: Optional[int] = None,
) -> list:
    results = []
    index = 0
    last_count = 0
    last_reported_count = 0
    last_growth_at = asyncio.get_event_loop().time()
    deadline = last_growth_at + max_seconds
    last_status_at = 0

    while asyncio.get_event_loop().time() < deadline:
        visible_count = await _available_card_count(page)
        reported_count = await _get_result_count(page)
        loader_idle = await _results_loader_is_idle(page)
        now = asyncio.get_event_loop().time()

        # Cap visible_count at max_cards so the loop never runs past it.
        if max_cards is not None:
            visible_count = min(visible_count, max_cards)

        if reported_count > last_reported_count:
            last_reported_count = reported_count
            last_growth_at = now
            await emit(f"📦 Kappal has reported {reported_count} rate card(s) so far ...")

        if visible_count > last_count:
            last_count = visible_count
            last_growth_at = now
            await emit(f"📦 {visible_count} rate card(s) available so far ...")

        if index < visible_count:
            await emit(f"  ↳ Card {index + 1} / {visible_count}+ ...")
            results.append(await _scrape_one_card(page, index))
            index += 1
            continue

        if index > 0 and now - last_growth_at >= quiet_seconds and loader_idle:
            if max_cards is not None and index < max_cards:
                await emit(
                    f"⏳ Kappal reported {max_cards} card(s); "
                    f"waiting for remaining {max_cards - index} to become ready ..."
                )
                last_growth_at = now
            else:
                await emit(f"✅ Loader finished and card list stayed stable for {quiet_seconds}s; scraped {len(results)} card(s).")
                return results

        if now - last_status_at >= 15:
            last_status_at = now
            if max_cards is not None:
                await emit(
                    f"⏳ Waiting for cards ... ready={visible_count}/{max_cards}, scraped={index}/{max_cards}"
                )
            else:
                loader_state = "idle" if loader_idle else "loading"
                await emit(
                    f"⏳ Waiting for cards ... reported={reported_count}, "
                    f"ready={visible_count}, scraped={index}, loader={loader_state}"
                )

        await asyncio.sleep(2)

    await emit(f"⚠️  Reached max scrape wait; returning {len(results)} scraped card(s).")
    return results


async def _scrape_one_card(page: Page, index: int) -> dict:
    try:
        btns = _view_details_buttons(page)
        unique_button_count = await _text_view_details_count(page)
        if unique_button_count == 0 and await btns.count() > index:
            btn = btns.nth(index)
        else:
            btn = None

        summary = {}
        if btn is not None:
            card_box = btn.locator(
                "xpath=ancestor::div[contains(@class,'card') or contains(@class,'Card') "
                "or contains(@class,'rate') or contains(@class,'Rate')][1]"
            )
            summary = await _extract_card_summary(card_box)

        if btn is not None:
            await btn.scroll_into_view_if_needed()
            await btn.click()
        else:
            await _click_view_details(page, index)

        await page.wait_for_selector(
            "[role='dialog'], .modal, [class*='Modal'], [class*='details-modal']",
            timeout=10_000,
        )
        await asyncio.sleep(0.6)
        details = await _extract_modal(page)
        return {**summary, **details}

    except Exception as exc:
        return {"_error": str(exc), "_card_index": index}

    finally:
        await _close_modal(page)
        await asyncio.sleep(0.5)


async def _available_card_count(page: Page) -> int:
    text_count = await _text_view_details_count(page)
    if text_count:
        return text_count
    if not await _results_loader_is_idle(page):
        return 0
    return await _deep_view_details_count(page)


async def _results_loader_is_idle(page: Page) -> bool:
    try:
        return bool(await page.evaluate(
            """
            () => {
                const visible = el => {
                    if (!el) return false;
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && Number(style.opacity || 1) > 0.05
                        && rect.width > 0
                        && rect.height > 0;
                };
                const loaderSelectors = [
                    '#toolbar-progress',
                    '.loader-spin',
                    '.md-mode-indeterminate',
                    'md-progress-linear',
                    'md-progress-linear *',
                    '.progress-linear',
                    '[class*="progress"]',
                    '[class*="loader"]',
                    '[class*="loading"]'
                ];
                const selectorLoader = loaderSelectors.some(sel => Array.from(document.querySelectorAll(sel)).some(visible));
                const topBlueLoader = Array.from(document.querySelectorAll('div, span, md-progress-linear, md-progress-linear *'))
                    .some(el => {
                        if (!visible(el)) return false;
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        const bg = style.backgroundColor || '';
                        const isTopBar = rect.top >= -2 && rect.top <= 180 && rect.height >= 2 && rect.height <= 10 && rect.width >= 120;
                        const looksBlue = /rgb\\(33,\\s*150,\\s*243\\)|rgb\\(25,\\s*118,\\s*210\\)|rgb\\(59,\\s*130,\\s*246\\)|rgb\\(37,\\s*99,\\s*235\\)/.test(bg);
                        const animated = style.transform !== 'none' || style.animationName !== 'none' || style.transitionProperty !== 'all';
                        return isTopBar && (looksBlue || animated);
                    });
                const fetchingText = /Fetching\\s+in\\s+progress/i.test(document.body?.innerText || '');
                return !(selectorLoader || topBlueLoader || fetchingText);
            }
            """
        ))
    except Exception:
        return True


def _view_details_buttons(page: Page) -> Locator:
    return page.locator(
        "button, a, md-button, [role='button'], [class*='button'], [class*='Button'], [class*='btn'], [class*='Btn']",
        has_text=re.compile(r"View\s*Details", re.I),
    )


async def _text_view_details_count(page: Page) -> int:
    return int(await page.evaluate(
        """
        () => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || 1) > 0.05
                    && rect.width > 0
                    && rect.height > 0;
            };
            const enabled = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const cls = String(el.className || '').toLowerCase();
                return !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && !cls.includes('disabled')
                    && style.pointerEvents !== 'none';
            };
            const clickableAncestor = (el) => {
                let cur = el;
                while (cur && cur !== document.documentElement) {
                    const role = (cur.getAttribute('role') || '').toLowerCase();
                    const tag = cur.tagName.toLowerCase();
                    if (tag === 'button' || tag === 'a' || tag === 'md-button' || role === 'button') {
                        return cur;
                    }
                    cur = cur.parentElement;
                }
                cur = el;
                while (cur && cur !== document.documentElement) {
                    const cls = String(cur.className || '');
                    const cursor = getComputedStyle(cur).cursor;
                    if (
                        cur.onclick ||
                        /(^|\\s)(btn|button)|Button|Btn|mat-button|md-button/.test(cls) ||
                        cursor === 'pointer'
                    ) return cur;
                    cur = cur.parentElement;
                }
                return el;
            };
            const cardKey = (target) => {
                let cur = target;
                let best = target;
                while (cur && cur !== document.documentElement) {
                    const rect = cur.getBoundingClientRect();
                    const text = (cur.innerText || cur.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (
                        visible(cur)
                        && rect.width >= 420
                        && rect.height >= 70
                        && rect.height <= 700
                        && /View\\s+Details/i.test(text)
                        && (/Freight\\s+Rate/i.test(text) || /Total\\s+Rate/i.test(text) || /Sailing\\s+Date/i.test(text) || /Effective\\s+Period/i.test(text) || /Proceed/i.test(text))
                    ) {
                        best = cur;
                    }
                    cur = cur.parentElement;
                }
                const rect = best.getBoundingClientRect();
                return [
                    Math.round(rect.top / 8),
                    Math.round(rect.left / 8),
                    Math.round(rect.width / 8),
                    Math.round(rect.height / 8),
                ].join(':');
            };
            const found = [];
            const seen = new Set();
            const visit = (node) => {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                if (/^View\\s+Details$/i.test(text) && visible(node)) {
                    const target = clickableAncestor(node);
                    const key = target ? cardKey(target) : '';
                    if (target && visible(target) && enabled(target) && key && !seen.has(key)) {
                        seen.add(key);
                        found.push(target);
                    }
                }
                if (node.shadowRoot) {
                    for (const child of node.shadowRoot.children || []) visit(child);
                }
                for (const child of node.children || []) visit(child);
            };
            visit(document.documentElement);
            return found.length;
        }
        """
    ))


async def _deep_view_details_count(page: Page) -> int:
    return int(await page.evaluate(
        """
        () => {
            const els = [];
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || 1) > 0.05
                    && rect.width > 0
                    && rect.height > 0;
            };
            const enabled = (el) => {
                const style = getComputedStyle(el);
                const cls = String(el.className || '').toLowerCase();
                return !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && !cls.includes('disabled')
                    && style.pointerEvents !== 'none';
            };
            const visit = (node) => {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                const text = (node.innerText || node.textContent || '').trim();
                if (/^View\\s+Details$/i.test(text) && visible(node) && enabled(node)) els.push(node);
                if (node.shadowRoot) for (const child of node.shadowRoot.children || []) visit(child);
                for (const child of node.children || []) visit(child);
            };
            visit(document.documentElement);
            return els.length;
        }
        """
    ))


async def _deep_click_view_details(page: Page, index: int):
    clicked = await page.evaluate(
        """
        (index) => {
            const els = [];
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || 1) > 0.05
                    && rect.width > 0
                    && rect.height > 0;
            };
            const enabled = (el) => {
                const style = getComputedStyle(el);
                const cls = String(el.className || '').toLowerCase();
                return !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && !cls.includes('disabled')
                    && style.pointerEvents !== 'none';
            };
            const visit = (node) => {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                const text = (node.innerText || node.textContent || '').trim();
                if (/^View\\s+Details$/i.test(text) && visible(node) && enabled(node)) els.push(node);
                if (node.shadowRoot) for (const child of node.shadowRoot.children || []) visit(child);
                for (const child of node.children || []) visit(child);
            };
            visit(document.documentElement);
            const el = els[index];
            if (!el) return false;
            el.scrollIntoView({block: 'center', inline: 'center'});
            el.click();
            return true;
        }
        """,
        index,
    )
    if not clicked:
        raise RuntimeError(f"Could not find View Details button #{index + 1}")


async def _click_view_details(page: Page, index: int):
    clicked = await page.evaluate(
        """
        (index) => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || 1) > 0.05
                    && rect.width > 0
                    && rect.height > 0;
            };
            const enabled = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const cls = String(el.className || '').toLowerCase();
                return !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && !cls.includes('disabled')
                    && style.pointerEvents !== 'none';
            };
            const clickableAncestor = (el) => {
                let cur = el;
                while (cur && cur !== document.documentElement) {
                    const role = (cur.getAttribute('role') || '').toLowerCase();
                    const tag = cur.tagName.toLowerCase();
                    if (tag === 'button' || tag === 'a' || tag === 'md-button' || role === 'button') {
                        return cur;
                    }
                    cur = cur.parentElement;
                }
                cur = el;
                while (cur && cur !== document.documentElement) {
                    const cls = String(cur.className || '');
                    const cursor = getComputedStyle(cur).cursor;
                    if (
                        cur.onclick ||
                        /(^|\\s)(btn|button)|Button|Btn|mat-button|md-button/.test(cls) ||
                        cursor === 'pointer'
                    ) return cur;
                    cur = cur.parentElement;
                }
                return el;
            };
            const cardKey = (target) => {
                let cur = target;
                let best = target;
                while (cur && cur !== document.documentElement) {
                    const rect = cur.getBoundingClientRect();
                    const text = (cur.innerText || cur.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (
                        visible(cur)
                        && rect.width >= 420
                        && rect.height >= 70
                        && rect.height <= 700
                        && /View\\s+Details/i.test(text)
                        && (/Freight\\s+Rate/i.test(text) || /Total\\s+Rate/i.test(text) || /Sailing\\s+Date/i.test(text) || /Effective\\s+Period/i.test(text) || /Proceed/i.test(text))
                    ) {
                        best = cur;
                    }
                    cur = cur.parentElement;
                }
                const rect = best.getBoundingClientRect();
                return [
                    Math.round(rect.top / 8),
                    Math.round(rect.left / 8),
                    Math.round(rect.width / 8),
                    Math.round(rect.height / 8),
                ].join(':');
            };
            const found = [];
            const seen = new Set();
            const visit = (node) => {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                if (/^View\\s+Details$/i.test(text) && visible(node)) {
                    const target = clickableAncestor(node);
                    const key = target ? cardKey(target) : '';
                    if (target && visible(target) && enabled(target) && key && !seen.has(key)) {
                        seen.add(key);
                        found.push(target);
                    }
                }
                if (node.shadowRoot) {
                    for (const child of node.shadowRoot.children || []) visit(child);
                }
                for (const child of node.children || []) visit(child);
            };
            visit(document.documentElement);
            const target = found[index];
            if (!target) return false;
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.click();
            return true;
        }
        """,
        index,
    )
    if not clicked:
        await _deep_click_view_details(page, index)


# ─── Card summary (visible on results list) ───────────────────────────────────

async def _extract_card_summary(card: Locator) -> dict:
    summary = {
        "card_service":     None,
        "card_service_type": None,
        "card_carrier":     None,
        "card_sailing_date": None,
        "card_transit_time": None,
        "card_free_days":   None,
        "card_cargo_type":  None,
        "card_freight_rate": None,
        "card_total_rate":   None,
    }
    try:
        text = await card.inner_text()
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        # Sailing date  e.g. "09 Jun 2026"
        for line in lines:
            if re.match(r"\d{2}\s+[A-Za-z]{3}\s+\d{4}", line):
                summary["card_sailing_date"] = line
                break

        # Transit time  e.g. "40 Days"
        for line in lines:
            m = re.search(r"(\d+)\s*days?", line, re.I)
            if m:
                summary["card_transit_time"] = line.strip()
                break

        # Rates  e.g. "USD 2,465.11"
        usd_amounts = re.findall(r"USD\s*([\d,]+\.?\d*)", text)
        if len(usd_amounts) >= 1:
            summary["card_freight_rate"] = float(usd_amounts[0].replace(",", ""))
        if len(usd_amounts) >= 2:
            summary["card_total_rate"] = float(usd_amounts[1].replace(",", ""))

        # Cargo type  FAK
        if "FAK" in text:
            summary["card_cargo_type"] = "FAK"

        # Carrier name (look for known liners or grab from specific element)
        carrier_el = card.locator("[class*='carrier'], [class*='liner'], [class*='Carrier']")
        if await carrier_el.count() > 0:
            summary["card_carrier"] = (await carrier_el.first.inner_text()).strip()

        # Service type e.g. CY/CY
        m = re.search(r"(CY|DOOR|CFS)/(CY|DOOR|CFS)", text)
        if m:
            summary["card_service_type"] = m.group(0)

    except Exception as e:
        summary["_summary_error"] = str(e)

    return summary


# ─── Modal extraction ─────────────────────────────────────────────────────────

async def _extract_modal(page: Page) -> dict:
    modal = page.locator(
        "[role='dialog'], .modal, [class*='Modal'], [class*='details-modal']"
    ).first

    header   = await _extract_modal_header(modal)

    # Charges tab
    await _click_tab(modal, "Charges")
    await asyncio.sleep(0.4)
    charges  = await _extract_charges_tab(modal)

    # Remarks & Inclusions tab
    await _click_tab(modal, "Remarks")
    await asyncio.sleep(0.3)
    remarks  = await _extract_remarks_tab(modal)

    # Schedule tab
    await _click_tab(modal, "Schedule")
    await asyncio.sleep(0.3)
    schedule = await _extract_schedule_tab(modal)

    # Free Days tab
    await _click_tab(modal, "Free Days")
    await asyncio.sleep(0.3)
    free_days = await _extract_free_days_tab(modal)

    return {
        **header,
        "charges":              charges,
        "remarks_and_inclusions": remarks,
        "schedule":             schedule,
        "free_days":            free_days,
    }


async def _extract_modal_header(modal: Locator) -> dict:
    result = {
        "port_of_origin":             None,
        "port_of_loading":            None,
        "port_of_loading_name":       None,
        "transshipment_port":         None,
        "port_of_discharge":          None,
        "port_of_discharge_name":     None,
        "carrier":                    None,
        "service_name":               None,
        "service_type":               None,
        "origin_service_mode":        None,
        "destination_service_mode":   None,
        "transit_time":               None,
        "sailing_date":               None,
        "incoterms":                  None,
        "cargo_type":                 None,
        "commodity":                  None,
        "total_cost":                 None,
        "freight_subtotal":           None,
        "valid_from":                 None,
        "valid_to":                   None,
    }
    try:
        # Go back to Charges tab first so header is fully rendered
        await _click_tab(modal, "Charges")
        await asyncio.sleep(0.2)

        text = await modal.inner_text()
        fields = _modal_label_fields(text)

        result["port_of_origin"] = _port_code(fields.get("Port of Origin"))
        result["port_of_loading"] = _port_code(fields.get("Port of Loading"))
        result["port_of_loading_name"] = _port_name(fields.get("Port of Loading"))
        result["transshipment_port"] = _port_code(fields.get("Via") or fields.get("V/S"))
        result["port_of_discharge"] = _port_code(fields.get("Port Of Discharge"))
        result["port_of_discharge_name"] = _port_name(fields.get("Port Of Discharge"))
        result["carrier"] = fields.get("Liner/Carrier")
        result["service_type"] = fields.get("Service Type")
        result["origin_service_mode"] = _dash_to_none(fields.get("Origin Service mode"))
        result["destination_service_mode"] = _dash_to_none(fields.get("Destination Service mode"))
        result["transit_time"] = _dash_to_none(fields.get("Transit Time"))
        result["sailing_date"] = _dash_to_none(fields.get("Sailing Date"))
        result["incoterms"] = _dash_to_none(fields.get("Incoterms"))
        result["cargo_type"] = _dash_to_none(fields.get("Cargo Type"))
        result["commodity"] = _dash_to_none(fields.get("Commodity"))
        valid_from, valid_to = _effective_period_dates(fields.get("Effective Period"))
        result["valid_from"] = valid_from
        result["valid_to"] = valid_to

        # Sailing date
        m = re.search(r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})", text)
        if not result["sailing_date"] and m:
            result["sailing_date"] = m.group(1)

        # Transit time
        m = re.search(r"(\d+)\s*Days?", text, re.I)
        if not result["transit_time"] and m:
            result["transit_time"] = m.group(0)

        # Service mode  e.g. CY / CY
        modes = re.findall(r"\b(CY|DOOR|CFS)\b", text)
        if not result["origin_service_mode"] and len(modes) >= 2:
            result["origin_service_mode"]      = modes[0]
            result["destination_service_mode"] = modes[1]

        # Cargo type
        if not result["cargo_type"] and "FAK" in text:
            result["cargo_type"] = "FAK"

        # Total cost
        usd_amounts = re.findall(r"USD\s*([\d,]+\.\d{2})", text)
        if usd_amounts:
            result["total_cost"]       = float(usd_amounts[-1].replace(",", ""))
        if len(usd_amounts) >= 2:
            result["freight_subtotal"] = float(usd_amounts[-2].replace(",", ""))

        # Carrier  – look for named element
        carrier_el = modal.locator(
            "[class*='carrier'], [class*='Carrier'], [class*='liner'], [class*='Liner']"
        )
        if await carrier_el.count() > 0:
            result["carrier"] = (await carrier_el.first.inner_text()).strip()

    except Exception as e:
        result["_header_error"] = str(e)

    return result


def _modal_label_fields(text: str) -> dict:
    labels = [
        "Port of Origin",
        "Port of Loading",
        "V/S",
        "Via",
        "Liner/Carrier",
        "Service Type",
        "Origin Service mode",
        "Destination Service mode",
        "Transit Time",
        "Sailing Date",
        "Effective Period",
        "Port Of Discharge",
        "Incoterms",
        "Cargo Type",
        "Commodity",
    ]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields = {}
    label_set = {label.lower(): label for label in labels}
    for i, line in enumerate(lines):
        canonical = label_set.get(line.lower())
        if not canonical:
            continue
        values = []
        for next_line in lines[i + 1:]:
            if next_line.lower() in label_set:
                break
            if next_line in {"Schedule", "Charges", "Remarks & Inclusions", "T&C", "Free Days"}:
                break
            values.append(next_line)
        fields[canonical] = " ".join(values).strip() or None
    return fields


def _dash_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return None if cleaned in {"", "-"} else cleaned


def _port_code(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"\b([A-Z]{5})\b", value)
    return match.group(1) if match else value.strip()


def _port_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    if "/" in cleaned:
        name = cleaned.split("/", 1)[1].strip()
        return name if _looks_like_port_name(name) else None
    without_code = re.sub(r"\b[A-Z]{5}\b", "", cleaned).strip(" -/")
    return without_code if _looks_like_port_name(without_code) else None


def _looks_like_port_name(value: Optional[str]) -> bool:
    name = (value or "").strip()
    if not name:
        return False
    if re.fullmatch(r"S\s+[A-Z]{5}", name):
        return False
    if re.fullmatch(r"[A-Z]{5}", name):
        return False
    return True


def _effective_period_dates(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    dates = re.findall(r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}", value)
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], None
    return None, None


async def _extract_charges_tab(modal: Locator) -> dict:
    """
    Extracts charge line items grouped into freight / origin / destination.

    Kappal renders each charge as a row of <input> elements (not <td>).
    Section headers ("Freight", "Origin Charges", "Destination Charges") are
    plain text nodes in separate container divs BEFORE their charge rows.

    Strategy: traverse the modal DOM in document order. Track the current
    section as we encounter section-header elements. Assign each charge row
    to whichever section is active at that point.
    """
    sections = {
        "freight":     {"subtotal": None, "line_items": []},
        "origin":      {"subtotal": None, "line_items": []},
        "destination": {"subtotal": None, "line_items": []},
    }

    try:
        js_result = await modal.evaluate("""
            (modalEl) => {
                const txt   = el => el ? (el.innerText || el.textContent || '').trim() : '';
                const val   = el => el ? (el.value   || el.innerText || el.textContent || '').trim() : '';

                // ── Identify visible section boundaries by vertical position ─
                const allElements = Array.from(modalEl.querySelectorAll('*'));

                const isSectionHeader = (el) => {
                    if (el.querySelectorAll('input').length > 0) return null;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0 || rect.height > 140) return null;
                    const t = txt(el).replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 160) return null;
                    if (/^freight(?:\\s+sub\\s*total\\b.*)?$/i.test(t)) return 'freight';
                    if (/^origin\\s+charges?(?:\\s+sub\\s*total\\b.*)?$/i.test(t)) return 'origin';
                    if (/^destination\\s+charges?(?:\\s+sub\\s*total\\b.*)?$/i.test(t)) return 'destination';
                    return null;
                };

                const boundaries = [];
                for (const el of allElements) {
                    const sec = isSectionHeader(el);
                    if (!sec) continue;
                    const rect = el.getBoundingClientRect();
                    boundaries.push({el, sec, top: rect.top});
                }
                boundaries.sort((a, b) => a.top - b.top);

                const deduped = [];
                for (const boundary of boundaries) {
                    const prev = deduped[deduped.length - 1];
                    if (prev && prev.sec === boundary.sec && Math.abs(prev.top - boundary.top) < 8) continue;
                    deduped.push(boundary);
                }

                // Subtotals: look for "Sub Total USD 3,233.00" patterns near each header
                const subtotals = {};
                for (const {el, sec} of deduped) {
                    const parent = el.closest('[class]') || el.parentElement;
                    if (parent) {
                        const pt = parent.innerText || '';
                        const m = pt.match(/Sub\\s*Total[^A-Z]*([A-Z]{3})\\s*([\\d,]+\\.\\d+)/i);
                        if (m && !subtotals[sec])
                            subtotals[sec] = {currency: m[1], amount: parseFloat(m[2].replace(/,/g,''))};
                    }
                }

                const getSectionFor = (el) => {
                    let current = 'freight';
                    const rowTop = el.getBoundingClientRect().top;
                    for (const {sec, top} of deduped) {
                        if (top <= rowTop + 2) current = sec;
                    }
                    return current;
                };

                // ── Extract charge rows ────────────────────────────────────
                const isChargeName = (v) => {
                    if (!v || v.length < 4) return false;
                    if (/^[A-Z]{3}$/.test(v)) return false;
                    if (/^[\\d,\\.]+$/.test(v)) return false;
                    if (/^\\d{2}(GP|HC|HQ|DV|RF|OT|RE)/i.test(v)) return false;
                    if (/^\\d+\\.\\d+$/.test(v)) return false;
                    if (/^(per |per$)/i.test(v)) return false;
                    if (/^(charges|basis|equipment type|amount|comments|quantity|sub total|total cost)/i.test(v)) return false;
                    return true;
                };

                const sections = {freight: [], origin: [], destination: []};
                const visitedRows = new Set();

                for (const inp of modalEl.querySelectorAll('input')) {
                    const v = val(inp);
                    if (!isChargeName(v)) continue;

                    // Walk up to find the row container
                    let row = inp.parentElement;
                    for (let i = 0; i < 8 && row; i++) {
                        const rowInputs = row.querySelectorAll('input');
                        const rt = txt(row);
                        if (rowInputs.length >= 2 && /\\bper\\b/i.test(rt) && /[A-Z]{3}/.test(rt)) break;
                        row = row.parentElement;
                    }
                    if (!row || visitedRows.has(row)) continue;
                    visitedRows.add(row);

                    const section = getSectionFor(row);
                    if (!sections[section]) continue;

                    // Read inputs
                    const rowInputs = Array.from(row.querySelectorAll('input'));
                    const inputVals = rowInputs.map(i => val(i)).filter(Boolean);

                    const basis = inputVals.find(x => /\\bper\\b/i.test(x)) || null;

                    let equip = null;
                    for (const s of row.querySelectorAll('select')) {
                        const sv = txt(s);
                        if (/^\\d{2}(GP|HC|HQ|DV|RF|OT|RE)/i.test(sv)) { equip = sv; break; }
                    }
                    if (!equip) equip = inputVals.find(x => /^\\d{2}(GP|HC|HQ|DV|RF|OT|RE)/i.test(x)) || null;

                    const qty = inputVals.find(x => /^\\d+\\.\\d+$/.test(x)) || null;

                    // Money pairs: find currency-code leaves, then adjacent amount
                    const moneyPairs = [];
                    for (const cEl of row.querySelectorAll('*')) {
                        const ct = txt(cEl);
                        if (!/^[A-Z]{3}$/.test(ct) || cEl.children.length > 0) continue;
                        const candidates = [
                            cEl.nextElementSibling,
                            cEl.parentElement && cEl.parentElement.nextElementSibling,
                        ].filter(Boolean);
                        for (const c of candidates) {
                            const amtEl = c.querySelector('input') || c;
                            const amtRaw = val(amtEl);
                            if (/^[\\d,]+\\.\\d+$/.test(amtRaw)) {
                                moneyPairs.push({currency: ct, amount: parseFloat(amtRaw.replace(/,/g,''))});
                                break;
                            }
                        }
                    }
                    if (moneyPairs.length === 0) {
                        const amtInputs = rowInputs.filter(i => {
                            const av = val(i);
                            return /^[\\d,]+\\.\\d+$/.test(av) && parseFloat(av.replace(/,/g,'')) > 0;
                        });
                        for (const ai of amtInputs)
                            moneyPairs.push({currency: null, amount: parseFloat(val(ai).replace(/,/g,''))});
                    }

                    const name = v.replace(/\\s+\\d{2}(GP|HC|HQ|DV|RF|OT|RE)\\s*$/i, '').trim();

                    sections[section].push({name, basis, equipment_type: equip, quantity: qty,
                        unit_price: moneyPairs[0] || null,
                        amount:     moneyPairs[moneyPairs.length - 1] || null});
                }

                return {sections, subtotals, boundaries: deduped.map(b => ({section: b.sec, top: b.top}))};
            }
        """)

        if js_result and js_result.get("sections"):
            for sec in ("freight", "origin", "destination"):
                sections[sec]["line_items"] = js_result["sections"].get(sec) or []
            for sec, sub in (js_result.get("subtotals") or {}).items():
                if sec in sections and sub:
                    sections[sec]["subtotal"] = sub

        _sanitize_charge_sections(sections)

        modal_text = await modal.inner_text()
        parsed = _extract_charges_from_text(
            modal_text,
            {
                "freight": {"subtotal": None, "line_items": []},
                "origin": {"subtotal": None, "line_items": []},
                "destination": {"subtotal": None, "line_items": []},
            },
        )
        for sec in ("freight", "origin", "destination"):
            parsed_items = parsed.get(sec, {}).get("line_items") or []
            if parsed_items:
                sections[sec]["line_items"] = parsed_items
                sections[sec]["subtotal"] = sections[sec]["subtotal"] or parsed[sec].get("subtotal")

        _sanitize_charge_sections(sections)
        _dedupe_charge_sections(sections)

    except Exception as e:
        sections["_error"] = str(e)

    return sections


def _section_header_present(text: str, section: str) -> bool:
    if section == "origin":
        return bool(re.search(r"\borigin\s+charges?\b", text or "", re.I))
    if section == "destination":
        return bool(re.search(r"\bdestination\s+charges?\b", text or "", re.I))
    return bool(re.search(r"\bfreight\b", text or "", re.I))


def _dedupe_charge_sections(sections: dict) -> None:
    non_freight_names = {
        _charge_identity(item)
        for sec in ("origin", "destination")
        for item in sections.get(sec, {}).get("line_items", [])
        if _charge_identity(item)
    }
    if not non_freight_names:
        return
    freight = sections.get("freight", {})
    freight["line_items"] = [
        item for item in freight.get("line_items", [])
        if _charge_identity(item) not in non_freight_names
    ]


def _charge_identity(item: dict) -> str:
    name = (item or {}).get("name") or ""
    return re.sub(r"\s+", " ", name).strip().lower()


def _sanitize_charge_sections(sections: dict) -> None:
    for sec in ("freight", "origin", "destination"):
        section = sections.get(sec)
        if not isinstance(section, dict):
            continue
        cleaned = []
        seen = set()
        for item in section.get("line_items") or []:
            name = _clean_charge_name((item or {}).get("name"))
            if not _looks_like_charge_name(name):
                continue
            item["name"] = name
            ident = _charge_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            cleaned.append(item)
        section["line_items"] = cleaned


def _clean_charge_name(value: Optional[str]) -> str:
    name = str(value or "").replace("\t", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _looks_like_charge_name(value: Optional[str]) -> bool:
    name = _clean_charge_name(value)
    low = name.lower()
    if not name or low in {"undefined", "free days", "total"}:
        return False
    if "\t" in str(value or ""):
        return False
    if len(name) < 4 or len(name) > 90:
        return False
    if not re.search(r"[A-Za-z]", name):
        return False
    if re.fullmatch(r"[A-Z]{3}", name):
        return False
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", name):
        return False
    if re.fullmatch(r"\d{2}\s*(GP|HC|HQ|DV|RF|OT|RE)", name, re.I):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", name):
        return False
    if re.fullmatch(r"per(?:\s+\w+){0,3}", name, re.I):
        return False
    if low in {"charges", "basis", "equipment type", "quantity", "quantity | slab", "unit price", "amount", "comments", "sub total", "total cost"}:
        return False
    if "basis equipment type" in low or "unit price amount" in low:
        return False
    return True


def _extract_charges_from_text(text: str, sections: dict) -> dict:
    current = "freight"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        low = line.lower()
        if low == "freight" or low.startswith("freight "):
            current = "freight"
            continue
        if "origin charges" in low:
            current = "origin"
            continue
        if "destination charges" in low:
            current = "destination"
            continue
        if "sub total" in low:
            amount = _parse_amount(" ".join(lines[i:i + 3]))
            if amount:
                sections[current]["subtotal"] = amount
            continue

        if not _looks_like_charge_name(line):
            continue

        window = _charge_line_window(lines, i)
        joined = " ".join(window)
        if not re.search(r"\b[A-Z]{3}\b", joined):
            continue
        if not re.search(r"\b(per equipment|per b/l|per bl|per document|per shipment)\b", joined, re.I):
            continue

        item = _parse_charge_line_window(window)
        if item and item["name"] not in {x["name"] for x in sections[current]["line_items"]}:
            sections[current]["line_items"].append(item)

    return sections


def _charge_line_window(lines: list[str], start: int) -> list[str]:
    window = [lines[start]]
    for line in lines[start + 1:start + 24]:
        low = line.lower()
        if low == "freight" or "origin charges" in low or "destination charges" in low:
            break
        if len(window) >= 7 and _looks_like_charge_name(line):
            break
        window.append(line)
    return window


def _parse_charge_line_window(lines: list[str]) -> Optional[dict]:
    name = _clean_charge_name(lines[0])
    if not _looks_like_charge_name(name):
        return None
    basis = next((x for x in lines if re.search(r"\bper\b", x, re.I)), None)
    equipment = next((x for x in lines if re.fullmatch(r"\d{2}\s*(GP|HC|HQ|DV|RF|OT|RE)", x, re.I)), None)
    quantity = next((x for x in lines if re.fullmatch(r"\d+(\.\d+)?", x)), None)
    amounts = []
    for i, line in enumerate(lines):
        if re.fullmatch(r"[A-Z]{3}", line.strip()) and i + 1 < len(lines):
            amount_text = f"{line} {lines[i + 1]}"
            amount = _parse_amount(amount_text)
            if amount:
                amounts.append(amount)
    if not amounts:
        return None
    return {
        "name": name,
        "basis": basis,
        "equipment_type": equipment,
        "quantity": quantity,
        "unit_price": amounts[0] if amounts else None,
        "amount": amounts[-1] if amounts else None,
    }


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


async def _extract_remarks_tab(modal: Locator) -> dict:
    """
    Reads only the Remarks and Inclusion text areas from the Remarks & Inclusions tab.
    Uses JS to find the rich-text editor content directly instead of the full modal text.
    """
    try:
        js_result = await modal.evaluate("""
            (modalEl) => {
                const txt = el => el ? (el.innerText || el.textContent || '').trim() : '';

                // The Remarks & Inclusions tab has two labelled sections:
                // "Remarks" and "Inclusion" — each followed by a rich-text editor div.
                // We find the label, then grab the next sibling editor's text.
                const getSection = (labelText) => {
                    for (const el of modalEl.querySelectorAll('*')) {
                        const t = txt(el);
                        if (t === labelText && el.children.length === 0) {
                            // Walk forward to find the editor container
                            let sib = el.nextElementSibling || (el.parentElement && el.parentElement.nextElementSibling);
                            for (let i = 0; i < 5 && sib; i++) {
                                const st = txt(sib);
                                if (st && st.length > 0 && sib !== el) return st;
                                sib = sib.nextElementSibling;
                            }
                        }
                    }
                    return null;
                };

                // Also try: find all contenteditable or .ql-editor divs (Quill editor)
                const editors = Array.from(modalEl.querySelectorAll(
                    '[contenteditable="true"], .ql-editor, [class*="editor"], [class*="rich-text"]'
                ));

                let remarks = getSection('Remarks') || '';
                let inclusions = getSection('Inclusion') || getSection('Inclusions') || '';

                // Fallback: first editor = remarks, second = inclusions
                if (!remarks && editors.length > 0) remarks = txt(editors[0]);
                if (!inclusions && editors.length > 1) inclusions = txt(editors[1]);

                return {remarks, inclusions};
            }
        """)

        return {
            "remarks":    (js_result.get("remarks")    or "").strip() or None,
            "inclusions": (js_result.get("inclusions") or "").strip() or None,
        }

    except Exception as e:
        return {"_error": str(e)}


async def _extract_schedule_tab(modal: Locator) -> list:
    schedule = []
    try:
        rows = modal.locator("tr")
        count = await rows.count()
        headers = []
        for i in range(count):
            cells = rows.nth(i).locator("td, th")
            cell_count = await cells.count()
            if cell_count == 0:
                continue
            values = [(await cells.nth(j).inner_text()).strip() for j in range(cell_count)]
            if i == 0 or all(v.isupper() or not v for v in values):
                headers = values
            else:
                if headers:
                    schedule.append(dict(zip(headers, values)))
                else:
                    schedule.append({f"col_{j}": values[j] for j in range(len(values))})
    except Exception as e:
        schedule.append({"_error": str(e)})
    return schedule


async def _extract_free_days_tab(modal: Locator) -> dict:
    try:
        text = await modal.inner_text()
        # Remove tab nav text
        for tab in ["Schedule", "Charges", "Remarks & Inclusions", "T&C", "Free Days"]:
            text = text.replace(tab, "")
        return {"raw": text.strip()}
    except Exception as e:
        return {"_error": str(e)}


# ─── Utilities ────────────────────────────────────────────────────────────────

async def _click_tab(modal: Locator, tab_name: str):
    tab = modal.locator(
        f"button:has-text('{tab_name}'), "
        f"[role='tab']:has-text('{tab_name}'), "
        f"a:has-text('{tab_name}'), "
        f"span:has-text('{tab_name}')"
    ).first
    if await tab.count() > 0:
        try:
            await tab.click()
            await asyncio.sleep(0.2)
        except Exception:
            pass


async def _close_modal(page: Page):
    close_btn = page.locator(
        "[aria-label='Close'], button:has-text('×'), button:has-text('✕'), "
        "[class*='close']:visible, [class*='Close']:visible, "
        "button[class*='modal']:visible"
    ).first
    try:
        if await close_btn.count() > 0:
            await close_btn.click()
        else:
            await _press_escape(page)
        await page.wait_for_selector(
            "[role='dialog'], .modal, [class*='Modal']",
            state="hidden",
            timeout=5_000,
        )
    except Exception:
        await _press_escape(page)
        await asyncio.sleep(0.5)


async def _press_escape(target):
    if hasattr(target, "keyboard"):
        await target.keyboard.press("Escape")
        return
    if hasattr(target, "page"):
        await target.page.keyboard.press("Escape")


def _parse_amount(text: str) -> Optional[dict]:
    """'USD 1,930.00'  →  {'currency': 'USD', 'amount': 1930.0}"""
    if not text or not text.strip():
        return None
    m = re.search(r"([A-Z]{3})\s*([\d,]+\.?\d*)", text.strip())
    if m:
        return {"currency": m.group(1), "amount": float(m.group(2).replace(",", ""))}
    # Bare number
    m2 = re.search(r"([\d,]+\.?\d+)", text.strip())
    if m2:
        return {"currency": None, "amount": float(m2.group(1).replace(",", ""))}
    return {"raw": text.strip()}
