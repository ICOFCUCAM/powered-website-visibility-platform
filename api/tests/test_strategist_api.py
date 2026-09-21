"""The Strategist over HTTP.

The endpoint streams, which means two things worth testing that nothing else
in this API needs: the events arrive as separate SSE frames rather than one
block at the end, and a failure halfway through has to become an event,
because the status line went out with the first byte and can no longer be a
500.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from api.ai import deps as ai_deps
from api.analysis.catalogue import seed
from api.tests.conftest import auth_headers
from api.tests.fake_model import FakeChatProvider, Scripted

SETTLED = date.today() - timedelta(days=4)


@pytest.fixture
async def assistant():
    """Installs a scripted model for one test. The real loop runs unchanged."""
    provider = FakeChatProvider()
    ai_deps.set_chat_provider(provider)
    try:
        yield provider
    finally:
        ai_deps.set_chat_provider(None)


@pytest.fixture
async def site(client, two_tenants, service_conn):
    await seed(service_conn)
    user, org = two_tenants["user_a"], two_tenants["org_a"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = uuid.UUID(created.json()["id"])
    await service_conn.execute(
        "insert into gsc_totals_daily (organization_id, website_id, date, clicks,"
        " impressions, position) values (%s,%s,%s,%s,%s,%s)",
        (org, website_id, SETTLED, 1234, 48219, 8.4),
    )
    return website_id, user, org, service_conn


async def ask(client, website_id, user, question, conversation_id=None):
    """Reads the stream frame by frame, the way a browser does."""
    events, buffer = [], ""
    async with client.stream(
        "POST",
        f"/api/v1/websites/{website_id}/ai/chat",
        json={
            "question": question,
            **(
                {"conversation_id": str(conversation_id)}
                if conversation_id
                else {}
            ),
        },
        headers=auth_headers(user),
    ) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if frame.startswith("data: "):
                    events.append(json.loads(frame[len("data: "):]))
    return events


# -- availability -----------------------------------------------------------
async def test_without_a_model_the_endpoint_says_so_rather_than_pretending(
    client, site
):
    website_id, user, *_ = site
    ai_deps.set_chat_provider(None)

    response = await client.post(
        f"/api/v1/websites/{website_id}/ai/chat",
        json={"question": "why?"},
        headers=auth_headers(user),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "assistant_unavailable"


async def test_the_screen_is_told_whether_the_assistant_exists(client, site, assistant):
    website_id, user, *_ = site
    body = (
        await client.get(
            f"/api/v1/websites/{website_id}/conversations",
            headers=auth_headers(user),
        )
    ).json()

    assert body["available"] is True
    # Opens with questions rather than a blank box: a chat that asks a
    # non-technical owner to think of a good question usually gets none.
    assert body["suggested_questions"][0] == "Why did my traffic change?"
    assert body["conversations"] == []


# -- the stream -------------------------------------------------------------
async def test_an_answer_arrives_as_a_stream_of_events(client, site, assistant):
    website_id, user, *_ = site
    assistant.turns = [
        Scripted(tools=[("get_performance_summary", {"period": "28d"})]),
        Scripted(text="You had 1,234 clicks in the last 28 days."),
    ]

    events = await ask(client, website_id, user, "how am I doing?")
    kinds = [e["type"] for e in events]

    assert kinds[0] == "conversation"
    assert "step" in kinds
    assert kinds[-1] == "done"
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "1,234 clicks" in text

    step = next(e for e in events if e["type"] == "step")
    assert step["label"] == "checking your search performance"


async def test_the_tool_saw_the_customer_s_real_figures(client, site, assistant):
    """The point of the whole feature: it answers from their data, not from
    what a model remembers about websites in general."""
    website_id, user, *_ = site
    assistant.turns = [
        Scripted(tools=[("get_performance_summary", {})]),
        Scripted(text="Here you go."),
    ]
    await ask(client, website_id, user, "how am I doing?")

    result = assistant.requests[1]["messages"][-1]["content"][0]["content"]
    assert "48219" in result or "48,219" in result
    assert "1234" in result


async def test_a_conversation_can_be_continued_and_read_back(client, site, assistant):
    website_id, user, *_ = site
    assistant.turns = [Scripted(text="Clicks are up."), Scripted(text="One query.")]

    first = await ask(client, website_id, user, "how am I doing?")
    conversation_id = first[0]["id"]
    await ask(client, website_id, user, "which query?", conversation_id)

    body = (
        await client.get(
            f"/api/v1/websites/{website_id}/conversations/{conversation_id}",
            headers=auth_headers(user),
        )
    ).json()
    assert [m["role"] for m in body["messages"]] == [
        "user", "assistant", "user", "assistant"
    ]
    assert body["messages"][1]["content"] == "Clicks are up."
    assert body["title"] == "how am I doing?"


async def test_a_conversation_can_be_deleted(client, site, assistant):
    website_id, user, *_ = site
    assistant.turns = [Scripted(text="Fine.")]
    events = await ask(client, website_id, user, "how am I doing?")
    conversation_id = events[0]["id"]

    deleted = await client.delete(
        f"/api/v1/websites/{website_id}/conversations/{conversation_id}",
        headers=auth_headers(user),
    )
    assert deleted.status_code == 204

    after = await client.get(
        f"/api/v1/websites/{website_id}/conversations/{conversation_id}",
        headers=auth_headers(user),
    )
    assert after.status_code == 404


# -- when it goes wrong -----------------------------------------------------
async def test_a_failure_mid_answer_becomes_an_event_not_a_500(
    client, site, assistant
):
    """The status line went out with the first byte. A browser that gets a
    stream which simply stops has nothing to show the customer."""
    website_id, user, *_ = site
    assistant.turns = [RuntimeError("the database went away")]

    events = await ask(client, website_id, user, "why?")
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "assistant_unavailable"


async def test_an_unknown_conversation_is_an_event_too(client, site, assistant):
    website_id, user, *_ = site
    assistant.turns = [Scripted(text="Fine.")]

    events = await ask(client, website_id, user, "why?", uuid.uuid4())
    assert events[-1]["code"] == "not_found"


async def test_an_empty_question_is_rejected_before_it_reaches_a_model(
    client, site, assistant
):
    website_id, user, *_ = site
    response = await client.post(
        f"/api/v1/websites/{website_id}/ai/chat",
        json={"question": ""},
        headers=auth_headers(user),
    )
    assert response.status_code == 422
    assert assistant.requests == []


# -- tenancy ----------------------------------------------------------------
async def test_another_organisation_cannot_ask_about_this_website(
    client, site, assistant
):
    website_id, *_ = site
    intruder = auth_headers(uuid.uuid4(), "intruder@example.com")

    response = await client.post(
        f"/api/v1/websites/{website_id}/ai/chat",
        json={"question": "how are they doing?"},
        headers=intruder,
    )
    assert response.status_code == 404
    assert assistant.requests == []

    listed = await client.get(
        f"/api/v1/websites/{website_id}/conversations", headers=intruder
    )
    assert listed.status_code == 404
