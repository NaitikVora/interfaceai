"""The synthetic LegacyCore application behaves like the legacy system it stands in for."""

from __future__ import annotations

import httpx

from tests.conftest import SUPERVISOR, TELLER, DemoServer


async def client(demo: DemoServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=demo.base_url, follow_redirects=False)


async def test_login_search_and_details_flow(demo: DemoServer) -> None:
    async with await client(demo) as c:
        assert (await c.get("/")).headers["location"] == "/login"
        bad = await c.post("/login", data={"operator_id": "teller1", "access_code": "nope"})
        assert bad.status_code == 401 and "Invalid operator ID or access code" in bad.text
        ok = await c.post("/login", data=TELLER)
        assert ok.status_code == 303 and ok.headers["location"] == "/home"
        assert "Main Menu" in (await c.get("/home")).text
        assert (
            "exactly 5 digits" in (await c.post("/members/search", data={"member_no": "12"})).text
        )
        missing = await c.post("/members/search", data={"member_no": "99999"})
        assert "No member found matching member number 99999" in missing.text
        found = await c.post("/members/search", data={"member_no": "12345"})
        assert found.headers["location"] == "/members/12345"
        details = (await c.get("/members/12345")).text
        assert "Member Details" in details and "$8,432.17" in details and "Demo Member" in details
        assert "data-testid" not in details  # deliberately hostile to test IDs


async def test_roles_and_subaccount_flow(demo: DemoServer) -> None:
    async with await client(demo) as c:
        await c.post("/login", data=TELLER)
        denied = await c.get("/members/12345/subaccounts/new")
        assert denied.status_code == 403 and "do not have permission" in denied.text
        await c.post("/login", data=SUPERVISOR)
        assert (await c.get("/members/12345/subaccounts/new")).status_code == 200
        invalid = await c.post(
            "/members/12345/subaccounts/new", data={"account_type": "Savings", "nickname": ""}
        )
        assert invalid.status_code == 422 and "Nickname is required" in invalid.text
        ok = await c.post(
            "/members/12345/subaccounts/new",
            data={"account_type": "Savings", "nickname": "Emergency Fund"},
        )
        assert ok.headers["location"].endswith("/review")
        review = (await c.get("/members/12345/subaccounts/review")).text
        assert "Confirm and Open Account" in review and "cannot be undone" in review
        confirmed = await c.post("/members/12345/subaccounts/confirm")
        assert "/confirmation/SA-" in confirmed.headers["location"]
        page = (await c.get(confirmed.headers["location"])).text
        assert "Sub-Account Opened" in page and "Confirmation Number" in page
        assert "Emergency Fund" in (await c.get("/members/12345")).text


async def test_fault_injection_modes(demo: DemoServer) -> None:
    async with await client(demo) as c:
        await c.post("/login", data=TELLER)
        await demo.inject(transient_search_failures=1)
        busy = await c.post("/members/search", data={"member_no": "12345"})
        assert busy.status_code == 503 and "temporarily unavailable" in busy.text
        assert 'value="12345"' in busy.text  # form value preserved for retry
        assert (await c.post("/members/search", data={"member_no": "12345"})).status_code == 303

        await demo.inject(interstitial="system_notice")
        notice = (await c.get("/members/search")).text
        assert "System Notice" in notice and "Acknowledge" in notice and "Main Menu" not in notice
        ack = await c.post("/notice/ack", data={"next": "/members/search"})
        assert ack.headers["location"] == "/members/search"
        evil = await c.post("/notice/ack", data={"next": "//evil.example"})
        assert evil.headers["location"] == "/home"
        assert "System Notice" not in (await c.get("/members/search")).text  # one-shot

        await demo.inject(app_error_on_search=True)
        error = await c.post("/members/search", data={"member_no": "12345"})
        assert error.status_code == 500 and "Application Error" in error.text
        await demo.reset()

        await c.post("/login", data=TELLER)
        await demo.inject(duplicate_search_form=True)
        assert (await c.get("/members/search")).text.count('value="Search"') == 2

        await demo.inject(expire_session_on_next_request=True)
        expired = await c.get("/members/search")
        assert expired.status_code == 303 and "reason=expired" in expired.headers["location"]
        assert "session has expired" in (await c.get("/login?reason=expired")).text

        state = (await c.get("/__admin/state")).json()
        assert state["flags"]["duplicate_search_form"] is True
