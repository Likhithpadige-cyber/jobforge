"""M22 acceptance at phone width (375 px): the screens used from an iPhone.

Simulated in Chromium at the iPhone viewport against the throwaway demo; a
real-device check over Tailscale remains part of the owner's acceptance.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.browser.conftest import MOBILE, Accounts, expect, no_horizontal_overflow

pytestmark = pytest.mark.e2e


def _usable(page: Page) -> None:
    no_horizontal_overflow(page)


def test_tracking_and_follow_ups_work_at_phone_width(signed_in, fresh: Accounts) -> None:  # type: ignore[no-untyped-def]
    page = signed_in("owner", MOBILE)

    page.goto("/")
    expect(page.get_by_test_id("follow-ups")).to_be_visible()
    _usable(page)

    page.goto(f"/applications/{fresh.ready_application_id}")
    form = page.get_by_role("form", name="Application tracking")
    form.scroll_into_view_if_needed()
    expect(form.get_by_label("Where it stands")).to_be_visible()
    expect(form.get_by_role("button", name="Save tracking")).to_be_visible()
    box = form.get_by_role("button", name="In 1 week").bounding_box()
    assert box is not None and box["height"] >= 44  # comfortable touch target
    _usable(page)

    page.goto("/applications")
    expect(page.get_by_label("Outcome")).to_be_visible()
    _usable(page)


def test_settings_sessions_card_at_phone_width(signed_in) -> None:  # type: ignore[no-untyped-def]
    page = signed_in("owner", MOBILE)
    page.goto("/settings")
    button = page.get_by_role("button", name="Sign out everywhere")
    expect(button).to_be_visible()
    button.click()  # opens the confirmation only; nothing is revoked
    expect(page.get_by_role("group", name="Confirm signing out everywhere")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(button).to_be_visible()
    _usable(page)
