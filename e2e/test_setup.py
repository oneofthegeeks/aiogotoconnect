"""Browser e2e for the setup page, ported step for step from goto-sms e2e/setup.spec.ts.

The tests share one example app and data dir, so they run in order on accumulated state.
"""

import json
import os
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from .conftest import ADMIN_PASSWORD, APP_PORT, FAKE_PORT

FAKE_SENT = f"http://127.0.0.1:{FAKE_PORT}/__sent"
APP = f"http://localhost:{APP_PORT}"
LOGIN = f"/login?password={ADMIN_PASSWORD}"
# Set SHOTS_DIR to also save screenshots of each view at phone and desktop widths.
SHOTS_DIR = os.environ.get("SHOTS_DIR")


def sent_count() -> int:
    with urllib.request.urlopen(FAKE_SENT) as res:
        return len(json.load(res))


def expect_no_horizontal_scroll(page: Page) -> None:
    """No sideways scrolling at this width."""
    fits = page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert fits is True


def shots(page: Page, name: str) -> None:
    """Screenshots of the current view at 375 and 1280 px, dark (default) and light (SHOTS_DIR only)."""
    if not SHOTS_DIR:
        return
    Path(SHOTS_DIR).mkdir(parents=True, exist_ok=True)
    size = page.viewport_size
    for theme in ("dark", "light"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        for width in (375, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            expect_no_horizontal_scroll(page)
            page.screenshot(path=f"{SHOTS_DIR}/{name}-{width}-{theme}.png", full_page=True)
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    if size:
        page.set_viewport_size(size)


@pytest.fixture
def admin(page: Page) -> Page:
    """A page signed in as the demo admin (the example's cookie login)."""
    page.goto(LOGIN)
    return page


def test_setup_steps_advance_from_credentials_to_a_sent_test_then_finish_shows_ready(
    admin: Page,
) -> None:
    page = admin
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.set_viewport_size({"width": 360, "height": 800})

    page.goto("/api/goto/")
    expect(page.locator("#step-1")).to_be_visible()
    expect(page.locator(".progress")).to_contain_text("Step 1 of 5")
    expect(page.locator("#step-2")).to_be_hidden()
    expect(page.locator(".rail-item").nth(1)).to_contain_text("Needs step 1")
    # The back link from the example's back_url.
    expect(page.locator("#back")).to_have_text("← Back to the app")
    expect(page.locator("#back")).to_have_attribute("href", "/")
    expect_no_horizontal_scroll(page)
    shots(page, "setup-step1")

    page.fill("#client-id", "cid")
    page.fill("#client-secret", "csecret")
    page.click("#save-cred")

    # Step 2 opens by itself, with focus on its heading.
    expect(page.locator("#step-2")).to_be_visible()
    expect(page.locator("#h-2")).to_be_focused()
    expect(page.locator(".rail-item").first).to_contain_text("Client ID cid, secret saved")
    shots(page, "setup-step2")

    # The fake GoTo approves at once and redirects back with ?connected=1: step 3, accounts loaded.
    page.click("#connect")
    expect(page.locator("#step-3")).to_be_visible()
    expect(page).to_have_url(f"{APP}/api/goto/")
    expect(page.locator("#account")).to_have_value("acct-1")
    expect(page.locator("#number")).to_have_value("+18015550100")
    expect(page.locator("#sms-hint")).to_contain_text("SMS is turned on")
    shots(page, "setup-step3")
    page.click("#save-number")

    expect(page.locator("#step-4")).to_be_visible()
    expect(page.locator("#h-4")).to_be_focused()
    shots(page, "setup-step4")
    page.fill("#test-to", "+18015550199")
    page.click("#send-test")
    expect(page.locator("#test-msg")).to_contain_text("Sent")
    assert sent_count() == 1
    page.click("#test-continue")

    expect(page.locator("#step-5")).to_be_visible()
    shots(page, "setup-step5")
    page.check('input[name="incoming"][value="polling"]')
    page.click("#finish")

    expect(page.locator("#manage")).to_be_visible()
    expect(page.locator("#ready-title")).to_contain_text("Ready")
    expect(page.locator("#ready-line")).to_contain_text("801-555-0100 (Test Account)")
    expect(page.locator("#ready-line")).to_contain_text("Incoming: Polling")
    expect(page.locator("#log-body tr")).to_have_count(1)
    expect_no_horizontal_scroll(page)

    page.set_viewport_size({"width": 1440, "height": 900})
    expect_no_horizontal_scroll(page)
    assert console_errors == []


def test_manage_a_reload_lands_on_ready_change_on_the_number_reopens_step_3(admin: Page) -> None:
    page = admin
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/api/goto/")
    expect(page.locator("#manage")).to_be_visible()
    expect(page.locator("#setup")).to_be_hidden()
    expect_no_horizontal_scroll(page)
    shots(page, "manage")

    page.get_by_role("button", name="Change sending number").click()
    expect(page.locator("#step-3")).to_be_visible()
    expect(page.locator("#h-3")).to_be_focused()
    expect(page.locator("#number")).to_have_value("+18015550100")
    expect(page.locator("#to-summary")).to_be_visible()
    # Saving a change goes back to the summary.
    page.click("#save-number")
    expect(page.locator("#manage")).to_be_visible()

    # The always-available test send.
    page.fill("#m-test-to", "(801) 555-0198")
    page.click("#m-send-test")
    expect(page.locator("#m-test-msg")).to_contain_text("Sent")
    expect_no_horizontal_scroll(page)
    page.set_viewport_size({"width": 360, "height": 800})
    expect_no_horizontal_scroll(page)
    # Valid description list: each row holds only dt and dd (Change lives in the dd).
    expect(page.locator("#summary-rows > div > :not(dt, dd)")).to_have_count(0)

    # An unsaved choice in step 5 is dropped: reopening shows the saved mode again.
    page.get_by_role("button", name="Change incoming texts").click()
    expect(page.locator('input[name="incoming"][value="polling"]')).to_be_checked()
    page.check('input[name="incoming"][value="off"]')
    page.click("#to-summary")
    expect(page.locator("#manage")).to_be_visible()
    page.get_by_role("button", name="Change incoming texts").click()
    expect(page.locator('input[name="incoming"][value="polling"]')).to_be_checked()

    # Back to summary after a Disconnect goes to the unfinished step, not a stale Ready card.
    # (Runs last: it leaves the app disconnected.)
    page.click("#to-summary")
    page.get_by_role("button", name="Change goto login").click()
    page.click("#disconnect")
    expect(page.locator("#connect-msg")).to_contain_text("Disconnected")
    page.click("#to-summary")
    expect(page.locator("#step-2")).to_be_visible()
    expect(page.locator("#manage")).to_be_hidden()


def test_theme_dark_by_default_the_toggle_switches_to_light_and_survives_a_reload(
    browser: Browser, base_url: str
) -> None:
    # A light OS preference must not matter: dark is the default.
    context = browser.new_context(color_scheme="light", base_url=base_url)
    page = context.new_page()
    page.goto(LOGIN)

    def background() -> str:
        return str(page.evaluate("getComputedStyle(document.body).backgroundColor"))

    page.set_viewport_size({"width": 320, "height": 700})
    page.goto("/api/goto/")
    expect(page.locator("#loading")).to_be_hidden()

    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert background() == "rgb(17, 19, 23)"  # --bg #111317
    to_light = page.get_by_role("button", name="Switch to light mode")
    expect(to_light).to_be_visible()
    expect_no_horizontal_scroll(page)

    to_light.focus()
    page.keyboard.press("Enter")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert background() == "rgb(242, 245, 244)"  # --bg #f2f5f4
    expect(page.get_by_role("button", name="Switch to dark mode")).to_be_visible()
    expect_no_horizontal_scroll(page)

    page.reload()
    expect(page.locator("#loading")).to_be_hidden()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert background() == "rgb(242, 245, 244)"

    page.get_by_role("button", name="Switch to dark mode").click()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    context.close()
