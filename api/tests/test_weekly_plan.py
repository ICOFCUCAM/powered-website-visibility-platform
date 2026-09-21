"""The weekly plan.

The line this milestone has to hold: SELECTION AND RANKING ARE CODE, PROSE IS
GENERATED. Most of these tests are about what happens when a generation tries
to cross that line — by reordering the priorities, by adding one, or by
putting a number in the prose that the customer's data does not contain.

In every case the answer is the same and is not negotiable: keep the ranking,
drop the prose, render the template, record the refusal.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from api.ai.metering import reusing
from api.ai.plan import WeeklyPlanService, week_start_for, window_for
from api.analysis.catalogue import seed
from api.tests.fake_model import FakeProvider

AS_OF = date(2026, 9, 21)  # a Monday


@pytest.fixture
async def website(service_conn, two_tenants):
    await seed(service_conn)
    await service_conn.execute("delete from issue_explanations")
    org = two_tenants["org_a"]
    row = await (
        await service_conn.execute(
            "insert into websites (organization_id, domain, canonical_url, name) "
            "values (%s,%s,%s,%s) returning id",
            (org, f"{uuid.uuid4().hex[:8]}.example", "https://example.com/", "Acme"),
        )
    ).fetchone()
    return org, row["id"], service_conn


async def add_issue(conn, org, website_id, type_key, *, impact, path=None):
    page_id, url = None, None
    if path:
        url = f"https://example.com{path}"
        page = await (
            await conn.execute(
                "insert into pages (organization_id, website_id, url, url_hash, "
                "  path) values (%s,%s,%s,sha256(%s),%s) "
                "on conflict (website_id, url_hash) "
                "do update set url = excluded.url returning id",
                (org, website_id, url, url.encode(), path),
            )
        ).fetchone()
        page_id = page["id"]
    await conn.execute(
        """
        insert into issues (organization_id, website_id, type_key, scope_type,
                            page_id, fingerprint, severity, impact_score,
                            evidence)
        values (%s,%s,%s,'page',%s,%s,'high',%s,%s)
        """,
        (org, website_id, type_key, page_id, uuid.uuid4().hex, impact,
         json.dumps({"url": url} if url else {})),
    )


def plan_service(conn, org, website_id, provider=None) -> WeeklyPlanService:
    return WeeklyPlanService(
        conn,
        organization_id=org,
        website_id=website_id,
        provider=provider,
        meter=reusing(conn),
    )


def generated(payload: dict) -> dict:
    """A well-behaved generation: same refs, same order, no new numbers."""
    return {
        "summary": "Two things moved this week.",
        "priorities": [
            {
                "ref": finding["ref"],
                "title": finding["suggested_title"],
                "why": f"This affects {finding['pages_affected']} pages.",
                "how": ["Open each page and fix it."],
            }
            for finding in payload["findings"]
        ],
    }


# -- ranking ----------------------------------------------------------------
async def test_priorities_are_ranked_by_measured_impact(website):
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=5, path="/a")
    await add_issue(conn, org, website_id, "thin_content", impact=90, path="/b")
    await add_issue(conn, org, website_id, "missing_h1", impact=40, path="/c")

    priorities = await plan_service(conn, org, website_id).rank()
    assert [p.type_key for p in priorities] == [
        "thin_content", "missing_h1", "missing_title"
    ]
    assert [p.rank for p in priorities] == [1, 2, 3]


async def test_at_most_four_priorities(website):
    """More than four is a list, and a list is what the customer already had."""
    org, website_id, conn = website
    for index, type_key in enumerate(
        ["missing_title", "thin_content", "missing_h1", "multiple_h1",
         "orphan_page", "no_structured_data"]
    ):
        await add_issue(conn, org, website_id, type_key, impact=100 - index,
                        path=f"/{index}")

    assert len(await plan_service(conn, org, website_id).rank()) == 4


async def test_the_same_evidence_produces_the_same_plan(website):
    """The milestone's determinism test. Regenerate on unchanged data and the
    priorities, their order and their impacts are identical — because nothing
    in that path asks a model anything."""
    org, website_id, conn = website
    # Two groups with the same impact and the same count, so only the final
    # tie-break on type key can separate them.
    await add_issue(conn, org, website_id, "missing_h1", impact=25, path="/a")
    await add_issue(conn, org, website_id, "multiple_h1", impact=25, path="/b")
    await add_issue(conn, org, website_id, "thin_content", impact=60, path="/c")

    service = plan_service(conn, org, website_id)
    first = await service.generate(as_of=AS_OF)
    second = await service.generate(as_of=AS_OF)

    def shape(result):
        return [
            (p.rank, p.type_key, p.count, p.impact, p.estimated_clicks_delta)
            for p in result.priorities
        ]

    assert shape(first) == shape(second)
    assert first.plan_id == second.plan_id


async def test_pages_with_the_same_problem_become_one_priority(website):
    org, website_id, conn = website
    for index in range(12):
        await add_issue(conn, org, website_id, "missing_title", impact=3,
                        path=f"/page-{index}")

    priorities = await plan_service(conn, org, website_id).rank()
    assert len(priorities) == 1
    assert priorities[0].count == 12
    assert priorities[0].title == "Write titles for 12 pages that have none"


# -- the line the model may not cross ---------------------------------------
async def test_a_well_behaved_generation_is_used(website):
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")
    provider = FakeProvider(responses=generated)

    result = await plan_service(conn, org, website_id, provider).generate(as_of=AS_OF)

    assert result.fallback_reason is None
    assert result.summary == "Two things moved this week."
    assert result.prose["f1"].source == "model"
    stored = await (
        await conn.execute(
            "select prose_source, body_md from recommendations where plan_id = %s",
            (result.plan_id,),
        )
    ).fetchone()
    assert stored["prose_source"] == "model"


async def test_a_reordered_generation_is_rejected(website):
    """Rule 2. If the model picked, the same website would produce a different
    top four on Tuesday than on Monday and neither could be explained."""
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=10, path="/a")
    await add_issue(conn, org, website_id, "thin_content", impact=90, path="/b")

    def reorder(payload):
        response = generated(payload)
        response["priorities"].reverse()
        return response

    result = await plan_service(
        conn, org, website_id, FakeProvider(responses=reorder)
    ).generate(as_of=AS_OF)

    assert result.fallback_reason == "reordered_priorities"
    # The ranking is untouched — it never depended on the generation.
    assert [p.type_key for p in result.priorities] == ["thin_content", "missing_title"]
    assert result.prose["f1"].source == "template"


async def test_a_generation_that_invents_a_priority_is_rejected(website):
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")

    def invent(payload):
        response = generated(payload)
        response["priorities"].append(
            {"ref": "f9", "title": "Buy backlinks", "why": "Trust me.",
             "how": ["Spend money."]}
        )
        return response

    result = await plan_service(
        conn, org, website_id, FakeProvider(responses=invent)
    ).generate(as_of=AS_OF)

    assert result.fallback_reason == "reordered_priorities"
    titles = await (
        await conn.execute(
            "select title from recommendations where plan_id = %s", (result.plan_id,)
        )
    ).fetchall()
    assert all(row["title"] != "Buy backlinks" for row in titles)


async def test_a_generation_with_an_invented_figure_is_rejected(website):
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")

    def poison(payload):
        response = generated(payload)
        response["priorities"][0]["why"] = "These pages get 8,400 searches a month."
        return response

    result = await plan_service(
        conn, org, website_id, FakeProvider(responses=poison)
    ).generate(as_of=AS_OF)

    assert result.fallback_reason == "unsupported_numbers"
    assert "8,400" not in result.prose["f1"].why
    status = await (
        await conn.execute(
            "select status from llm_calls where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert status["status"] == "refused"


async def test_the_plan_still_runs_when_the_ai_allowance_is_spent(website):
    """docs/06-ai-layer.md, explicitly: bulk explanations fall back, the
    weekly plan still runs, because it is the product."""
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")
    await conn.execute(
        "update organizations set monthly_ai_budget_usd = 0.01 where id = %s",
        (org,),
    )
    await conn.execute(
        "insert into llm_calls (organization_id, purpose, model, model_provider,"
        "  cost_usd, derived_from) values (%s,'issue_explanation','m','anthropic',"
        "  9.99, '{\"x\":1}')",
        (org,),
    )

    provider = FakeProvider(responses=generated)
    result = await plan_service(conn, org, website_id, provider).generate(as_of=AS_OF)

    assert provider.calls, "the weekly plan is the product and must still run"
    assert result.fallback_reason is None


# -- memory -----------------------------------------------------------------
async def test_last_week_is_carried_into_the_prompt(website):
    """The join that makes it a consultant rather than a report generator."""
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")

    service = plan_service(conn, org, website_id, FakeProvider(responses=generated))
    previous = await service.generate(as_of=AS_OF - timedelta(days=7))
    await conn.execute(
        "update recommendations set status = 'RESOLVED', resolved_at = now() "
        " where plan_id = %s",
        (previous.plan_id,),
    )

    provider = FakeProvider(responses=generated)
    await plan_service(conn, org, website_id, provider).generate(as_of=AS_OF)

    last_week = provider.calls[0].payload["last_week"]
    assert last_week["had_plan"] is True
    assert last_week["week_start"] == (
        week_start_for(AS_OF) - timedelta(days=7)
    ).isoformat()
    assert last_week["items"][0]["status"] == "RESOLVED"
    assert last_week["items"][0]["pages_now"] == 1


async def test_the_first_plan_says_there_is_nothing_to_compare(website):
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")

    result = await plan_service(conn, org, website_id).generate(as_of=AS_OF)
    assert "first plan" in result.summary


async def test_a_customers_status_survives_regeneration(website):
    """Marking something done must not be undone by the nightly job."""
    org, website_id, conn = website
    await add_issue(conn, org, website_id, "missing_title", impact=30, path="/a")

    service = plan_service(conn, org, website_id)
    first = await service.generate(as_of=AS_OF)
    await conn.execute(
        "update recommendations set status = 'DISMISSED' where plan_id = %s",
        (first.plan_id,),
    )
    await service.generate(as_of=AS_OF)

    row = await (
        await conn.execute(
            "select status from recommendations where plan_id = %s", (first.plan_id,)
        )
    ).fetchone()
    assert row["status"] == "DISMISSED"


# -- the window -------------------------------------------------------------
def test_the_window_is_the_dashboards_window():
    """Twenty-eight days ending at the last day Google has settled data for.
    If these two drift the email and the screen disagree, and a customer who
    finds that stops believing both."""
    start, end, prior_start, prior_end = window_for(AS_OF)
    assert (end - start).days == 27
    assert end == AS_OF - timedelta(days=3)
    assert prior_end == start - timedelta(days=1)
    assert (prior_end - prior_start).days == 27


def test_the_week_starts_on_monday():
    assert week_start_for(date(2026, 9, 24)) == date(2026, 9, 21)
    assert week_start_for(date(2026, 9, 21)) == date(2026, 9, 21)
