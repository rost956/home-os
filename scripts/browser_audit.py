"""Run the HTML route and viewport sweep against a local audit instance."""

from __future__ import annotations

import csv
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("BROWSER_AUDIT_BASE_URL", "http://127.0.0.1:8765")
USERNAME = os.environ["BROWSER_AUDIT_USERNAME"]
PASSWORD = os.environ["BROWSER_AUDIT_PASSWORD"]
OUTPUT = Path(os.getenv("BROWSER_AUDIT_OUTPUT", "artifacts/browser-audit"))
WIDTHS = (320, 360, 390, 768, 1280, 1440)
ROUTES = (
    "/", "/login", "/register", "/today", "/search", "/settings", "/backup",
    "/ai/settings", "/recipes", "/recipes/new", "/recipes/import", "/recipes/1",
    "/recipes/1/edit", "/menu", "/shopping", "/shopping/lists/1", "/expenses",
    "/expenses/analytics", "/expenses/categories/1/analytics", "/expenses/lists/1",
    "/expenses/categories/1", "/expenses/planning", "/expenses/splits", "/income",
    "/finance", "/chats", "/chats/1", "/wishlist", "/wishlist/shared/audituser",
    "/watch", "/moments", "/planner", "/vehicles", "/vehicles/new", "/vehicles/1",
    "/vehicles/1/edit", "/vehicles/1/log", "/vehicles/1/log/new", "/vehicles/1/log/print",
    "/vehicles/1/log/1", "/vehicles/1/log/1/edit", "/vehicles/1/maintenance",
    "/vehicles/1/maintenance/new", "/vehicles/1/maintenance/1/edit", "/vehicles/1/fuel",
    "/vehicles/1/fuel/new", "/vehicles/1/fuel/1/edit", "/vehicles/trips", "/fuel",
    "/fuel/add", "/fuel/settings", "/fuel/1", "/fuel/1/settings", "/vehicles/trips/1",
    "/files", "/files/new", "/files/1", "/share/audit-public-token-12345678901234567890",
)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        anonymous = browser.new_page(viewport={"width": 390, "height": 844})
        for route in ("/login", "/register"):
            for width in WIDTHS:
                anonymous.set_viewport_size(
                    {"width": width, "height": 844 if width <= 768 else 900}
                )
                response = anonymous.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
                for theme in ("light", "dark"):
                    anonymous.evaluate(
                        "theme => document.documentElement.dataset.theme = theme", theme
                    )
                    overflow = anonymous.evaluate(
                        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                    )
                    results.append(
                        {
                            "url": route,
                            "width": width,
                            "theme": theme,
                            "status": response.status if response else "no response",
                            "final_url": anonymous.url.removeprefix(BASE_URL),
                            "title": anonymous.title(),
                            "heading": anonymous.locator("h1").first.inner_text()
                            if anonymous.locator("h1").count()
                            else "",
                            "horizontal_overflow": overflow,
                        }
                    )
                    if response and response.status == 200 and width == 390 and theme == "light":
                        anonymous.screenshot(
                            path=str(OUTPUT / f"{route.strip('/').replace('/', '-')}-anonymous-390.png"),
                            full_page=True,
                        )
        anonymous.goto(f"{BASE_URL}/register")
        anonymous.locator("[name=username]").fill("audit-error-case")
        anonymous.locator("[name=password]").fill("audit-password-123")
        anonymous.locator("[name=password_confirm]").fill("different-password-123")
        anonymous.locator("button[type=submit]").click()
        registration_error = anonymous.locator(".alert").inner_text()
        anonymous.screenshot(path=str(OUTPUT / "register-validation-error.png"), full_page=True)
        anonymous.goto(f"{BASE_URL}/login")
        anonymous.locator("[name=username]").fill("missing-audit-user")
        anonymous.locator("[name=password]").fill("wrong-password-123")
        anonymous.locator("button[type=submit]").click()
        login_error = anonymous.locator(".alert").inner_text()
        assert registration_error and login_error
        anonymous.screenshot(path=str(OUTPUT / "login-validation-error.png"), full_page=True)
        anonymous.close()
        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
        page.locator("[name=username]").fill(USERNAME)
        page.locator("[name=password]").fill(PASSWORD)
        page.locator("button[type=submit]").click()
        page.wait_for_load_state("domcontentloaded")
        page.goto(f"{BASE_URL}/files/new")
        page.locator("[name=title]").fill("Audit attachment " + "long-name-" * 13)
        page.locator("[name=description]").fill("Temporary browser audit file; test data only.")
        page.locator("[name=files]").set_input_files(
            str(Path(__file__).resolve().parents[1] / "app/static/icon-192.png")
        )
        page.locator(".file-transfer-form button[type=submit]").click()
        page.wait_for_load_state("domcontentloaded")
        attachment_route = page.url.removeprefix(BASE_URL)
        public_url = page.locator("[data-share-link]").input_value()
        public_route = public_url.removeprefix(BASE_URL)
        public_page = browser.new_page(viewport={"width": 390, "height": 844})
        public_page.goto(public_url, wait_until="domcontentloaded")
        public_page.locator("[data-share-preview]").click()
        assert public_page.locator("[data-share-lightbox]").is_visible()
        public_page.screenshot(
            path=str(OUTPUT / "public-file-preview-open-390.png"), full_page=True
        )
        public_page.keyboard.press("Escape")
        assert not public_page.locator("[data-share-lightbox]").is_visible()
        public_page.screenshot(path=str(OUTPUT / "public-file-attachment-390.png"), full_page=True)
        public_page.close()
        routes_to_check = (*ROUTES, attachment_route, public_route)
        for route in routes_to_check:
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 844 if width <= 768 else 900})
                response = page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
                page.wait_for_timeout(40)
                for theme in ("light", "dark"):
                    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                    overflow = page.evaluate(
                        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                    )
                    results.append(
                        {
                            "url": route,
                            "width": width,
                            "theme": theme,
                            "status": response.status if response else "no response",
                            "final_url": page.url.removeprefix(BASE_URL),
                            "title": page.title(),
                            "heading": page.locator("h1").first.inner_text()
                            if page.locator("h1").count()
                            else "",
                            "horizontal_overflow": overflow,
                        }
                    )
                    if theme == "light" and (
                        width in (390, 1440) and route in ("/finance", "/fuel", "/vehicles/1", "/recipes")
                        or (route in ("/finance", "/fuel", "/moments") and width == 320)
                        or (route == "/vehicles/1/log" and width == 768)
                    ):
                        page.screenshot(
                            path=str(OUTPUT / f"{route.strip('/').replace('/', '-') or 'home'}-{width}.png"),
                            full_page=True,
                        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{BASE_URL}/finance?from_date=2026-10-01&to_date=2026-10-02")
        first_series = page.locator(".comparison-line.current-series")
        data_days = page.locator(".comparison-hit").count()
        mouse_target = page.locator(".comparison-hit").nth(2).bounding_box()
        page.mouse.move(
            mouse_target["x"] + mouse_target["width"] / 2,
            mouse_target["y"] + mouse_target["height"] / 2,
        )
        mouse_tooltip = page.locator(".comparison-tooltip").inner_text()
        page.locator(".comparison-hit").nth(1).focus()
        keyboard_tooltip = page.locator(".comparison-tooltip").inner_text()
        assert "\u0414\u0435\u043d\u044c 1" in keyboard_tooltip and "\u041f\u0440\u043e\u0448\u043b\u044b\u0439" in keyboard_tooltip
        assert "\u0414\u0435\u043d\u044c 2" in mouse_tooltip
        page.locator(".comparison-hit").last.focus()
        missing_series_tooltip = page.locator(".comparison-tooltip").inner_text()
        assert "\u043d\u0435\u0442 \u0442\u043e\u0447\u043a\u0438 \u0432 \u0442\u0435\u043a\u0443\u0449\u0435\u043c \u043f\u0435\u0440\u0438\u043e\u0434\u0435" in missing_series_tooltip
        assert first_series.count() == 1 and data_days >= 2
        page.evaluate("document.documentElement.dataset.theme = 'dark'")
        page.screenshot(path=str(OUTPUT / "finance-dark-390.png"), full_page=True)
        page.goto(f"{BASE_URL}/settings")
        page.locator('[data-color-picker="color_primary"]').evaluate(
            "picker => { picker.value = '#9f1239'; picker.dispatchEvent(new Event('input', {bubbles: true})); }"
        )
        custom_palette_color = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--primary').trim()"
        )
        assert custom_palette_color.lower() == "#9f1239"
        page.screenshot(path=str(OUTPUT / "settings-custom-palette-390.png"), full_page=True)
        page.goto(f"{BASE_URL}/finance?from_date=2026-10-01&to_date=2026-10-02")
        page.locator(".comparison-hit").first.focus()
        default_focus_fill = page.locator(".comparison-hit").first.evaluate(
            "element => getComputedStyle(element).fill"
        )
        page.evaluate("document.documentElement.style.setProperty('--primary', '#9f1239')")
        palette_focus_fill = page.locator(".comparison-hit").first.evaluate(
            "element => getComputedStyle(element).fill"
        )
        assert palette_focus_fill != default_focus_fill
        page.screenshot(path=str(OUTPUT / "finance-custom-palette-390.png"), full_page=True)
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        touch_page = mobile.new_page()
        touch_page.goto(f"{BASE_URL}/login")
        touch_page.locator("[name=username]").fill(USERNAME)
        touch_page.locator("[name=password]").fill(PASSWORD)
        touch_page.locator("button[type=submit]").click()
        touch_page.goto(f"{BASE_URL}/finance")
        touch_page.locator(".comparison-hit").nth(0).tap()
        touch_tooltip = touch_page.locator(".comparison-tooltip").inner_text()
        touch_page.set_viewport_size({"width": 844, "height": 390})
        touch_page.evaluate("document.documentElement.style.fontSize = '200%'")
        zoom_overflow = touch_page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        touch_page.screenshot(path=str(OUTPUT / "finance-rotation-text-200.png"), full_page=True)
        assert "\u0414\u0435\u043d\u044c 0" in touch_tooltip and not zoom_overflow, (
            touch_tooltip.encode("unicode_escape").decode("ascii"),
            zoom_overflow,
        )
        mobile.close()
        page.emulate_media(media="print")
        page.goto(f"{BASE_URL}/vehicles/1/log/print")
        page.screenshot(path=str(OUTPUT / "vehicle-log-print.png"), full_page=True)
        (OUTPUT / "interaction-results.txt").write_text(
            f"Finance filter retained period comparison; {data_days} aligned day targets.\n"
            f"Keyboard focus tooltip: {keyboard_tooltip}\nMouse hover tooltip: {mouse_tooltip}\n"
            f"Out-of-range series tooltip: {missing_series_tooltip}\n"
            f"Touch tooltip: {touch_tooltip}\nRotated viewport with 200% root font overflow: {zoom_overflow}\n"
            f"Settings palette preview: {custom_palette_color}\nGraph focus color with palette: {palette_focus_fill}\n"
            f"Registration form error state: {registration_error}\nLogin form error state: {login_error}\n"
            "Finance dark theme screenshot and vehicle log print view captured.\n",
            encoding="utf-8",
        )
        zoom_results: list[dict[str, object]] = []
        for route in routes_to_check:
            for width in (320, 390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 844 if width <= 768 else 900})
                page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                overflow_px = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                zoom_results.append({"url": route, "width": width, "overflow_px": overflow_px})
                if route == "/settings" and width == 320:
                    page.screenshot(path=str(OUTPUT / "settings-text-200-320.png"), full_page=True)
                if route == "/menu" and width == 320:
                    page.screenshot(path=str(OUTPUT / "menu-text-200-320.png"), full_page=True)
                if route == "/planner" and width == 768:
                    page.screenshot(path=str(OUTPUT / "planner-text-200-768.png"), full_page=True)
        with (OUTPUT / "text-zoom-results.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=zoom_results[0].keys())
            writer.writeheader()
            writer.writerows(zoom_results)
        zoom_failures = [row for row in zoom_results if row["overflow_px"] > 0]
        assert not zoom_failures, zoom_failures
        browser.close()

    with (OUTPUT / "viewport-results.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    failures = [row for row in results if row["status"] != 200 or row["horizontal_overflow"]]
    print(f"Checked {len(ROUTES)} authenticated routes plus 2 file-attachment states and "
          f"login/register at {len(WIDTHS)} widths "
          f"in both themes ({len(results)} browser viewport states).")
    print(f"Problems: {len(failures)}. Results: {OUTPUT / 'viewport-results.csv'}")
    for row in failures:
        print(row)


if __name__ == "__main__":
    main()
