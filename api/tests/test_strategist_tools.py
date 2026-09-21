"""The Strategist's tools.

Half of these are ordinary "does the query work" tests. The other half are the
ones that matter: the structural properties that stop a conversation reaching
another tenant's data or asking the database something nobody wrote down.

The model does not write SQL and cannot name a tenant. Both are asserted here
rather than trusted to a paragraph in a prompt.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from api.ai.tools import REGISTRY, ROW_CAP, StrategistScope, definitions, run_tool
from api.ai.tools.base import PERIODS, ToolError
from api.analysis.catalogue import seed

TODAY = date(2026, 9, 21)
SETTLED = TODAY - timedelta(days=3)


@pytest.fixture
async def site(service_conn, two_tenants):
    await seed(service_conn)
    # The same call the sync path makes before a backfill writes: the Google
    # fact tables are partitioned by month, and a missing partition is an
    # INSERT failure rather than a silent drop.
    await service_conn.execute(
        "select app.ensure_partitions_for_backfill(%s, %s)",
        (TODAY - timedelta(days=400), TODAY + timedelta(days=30)),
    )
    org, other = two_tenants["org_a"], two_tenants["org_b"]
    ids = {}
    for key, organization, domain in (
        ("mine", org, "mine.example"),
        ("theirs", other, "theirs.example"),
    ):
        row = await (
            await service_conn.execute(
                "insert into websites (organization_id, domain, canonical_url) "
                "values (%s,%s,%s) returning id",
                (organization, f"{uuid.uuid4().hex[:6]}-{domain}",
                 f"https://{domain}/"),
            )
        ).fetchone()
        ids[key] = (organization, row["id"])
    return ids, service_conn


async def add_totals(conn, org, website_id, day, clicks, impressions, position):
    await conn.execute(
        "insert into gsc_totals_daily (organization_id, website_id, date, clicks,"
        " impressions, position) values (%s,%s,%s,%s,%s,%s) "
        "on conflict (website_id, date) do update set clicks = excluded.clicks",
        (org, website_id, day, clicks, impressions, position),
    )


async def add_query(conn, org, website_id, day, phrase, clicks, impressions, position):
    await conn.execute(
        "insert into gsc_query_daily (organization_id, website_id, date,"
        " query_hash, query, clicks, impressions, position)"
        " values (%s,%s,%s,sha256(%s),%s,%s,%s,%s) on conflict do nothing",
        (org, website_id, day, phrase.encode(), phrase, clicks, impressions, position),
    )


def scope_for(org, website_id, domain="mine.example") -> StrategistScope:
    return StrategistScope(
        organization_id=org, website_id=website_id, domain=domain, as_of=TODAY
    )


# -- the structural properties ---------------------------------------------
def test_no_tool_lets_the_model_name_a_tenant():
    """The whole tenancy argument for this feature in one assertion.

    Scope comes from the session. If a schema ever grew an organization_id or
    website_id property, a conversation could ask about somebody else's site —
    and the prompt would be the only thing stopping it.
    """
    forbidden = {"organization_id", "website_id", "site_id", "org", "organisation_id"}
    offenders = {
        tool.name: sorted(set(tool.schema["properties"]) & forbidden)
        for tool in REGISTRY.values()
        if set(tool.schema["properties"]) & forbidden
    }
    assert offenders == {}


def test_no_tool_accepts_sql_or_a_table_name():
    """A model that emits SQL against a multi-tenant database is one prompt
    injection away from a cross-tenant leak."""
    forbidden = {"sql", "query_sql", "table", "column", "where", "order", "filter"}
    offenders = {
        tool.name: sorted(set(tool.schema["properties"]) & forbidden)
        for tool in REGISTRY.values()
        if set(tool.schema["properties"]) & forbidden
    }
    assert offenders == {}


def test_every_tool_refuses_unknown_arguments():
    for tool in REGISTRY.values():
        assert tool.schema["additionalProperties"] is False, tool.name


def test_every_tool_is_described_for_a_reader_who_cannot_see_the_code():
    for tool in REGISTRY.values():
        assert len(tool.description) > 40, tool.name
        assert tool.step_label and tool.step_label[0].islower(), tool.name


def test_the_tool_list_is_the_one_the_api_expects():
    definitions_ = definitions()
    assert len(definitions_) == len(REGISTRY)
    for entry in definitions_:
        assert set(entry) == {"name", "description", "input_schema"}


def test_a_window_is_chosen_from_an_allowlist():
    scope = scope_for(uuid.uuid4(), uuid.uuid4())
    start, end = scope.window("28d")
    assert end == SETTLED, "windows end at the last settled Google day"
    assert (end - start).days == 27

    with pytest.raises(ToolError):
        scope.window("all time")
    with pytest.raises(ToolError):
        scope.window("1900-01-01 to now")
    assert set(PERIODS) == {"7d", "28d", "90d", "180d"}


# -- scoping, against a real database --------------------------------------
async def test_a_tool_returns_nothing_from_another_organisation(site):
    """The belt. RLS is the braces, and the request path runs as the role it
    applies to — but a tool that forgot its website filter would leak within
    an organisation too, so the filter is tested directly."""
    ids, conn = site
    org, mine = ids["mine"]
    other_org, theirs = ids["theirs"]

    await add_query(conn, org, mine, SETTLED, "my search", 10, 100, 4.0)
    await add_query(conn, other_org, theirs, SETTLED, "their search", 999, 9999, 1.0)

    run = await run_tool(conn, scope_for(org, mine), "get_top_queries", {})
    labels = [row["label"] for row in run.result["queries"]]
    assert labels == ["my search"]


async def test_row_caps_apply_however_large_a_limit_is_asked_for(site):
    ids, conn = site
    org, mine = ids["mine"]
    for index in range(ROW_CAP + 20):
        await add_query(conn, org, mine, SETTLED, f"search {index}", index, 100, 4.0)

    run = await run_tool(
        conn, scope_for(org, mine), "get_top_queries", {"limit": 5000}
    )
    assert len(run.result["queries"]) == ROW_CAP


async def test_a_bad_argument_comes_back_as_a_result_not_an_exception(site):
    """A tool that raises out of the loop ends the customer's turn with
    nothing. Handed back, the model corrects itself and carries on."""
    ids, conn = site
    org, mine = ids["mine"]

    run = await run_tool(
        conn, scope_for(org, mine), "get_top_queries", {"period": "since 2019"}
    )
    assert run.error and "period" in run.error
    assert run.result is None


async def test_an_unknown_tool_is_a_result_too(site):
    ids, conn = site
    org, mine = ids["mine"]
    run = await run_tool(conn, scope_for(org, mine), "drop_everything", {})
    assert run.error == "No tool named drop_everything."


# -- the answers themselves -------------------------------------------------
async def test_the_summary_says_when_there_is_no_data_rather_than_zero(site):
    """Zero traffic and no connection look identical in the numbers and are
    completely different situations."""
    ids, conn = site
    org, mine = ids["mine"]

    run = await run_tool(conn, scope_for(org, mine), "get_performance_summary", {})
    assert run.result["has_data"] is False
    assert "not the same as zero" in run.result["note"]


async def test_the_summary_cites_its_window_and_what_it_compared_with(site):
    """The milestone's acceptance criterion begins here: a model can only cite
    a window it was told."""
    ids, conn = site
    org, mine = ids["mine"]
    for offset in range(56):
        day = SETTLED - timedelta(days=offset)
        await add_totals(conn, org, mine, day, 10 if offset < 28 else 20, 500, 8.0)

    run = await run_tool(conn, scope_for(org, mine), "get_performance_summary", {})
    result = run.result

    assert result["period"] == {
        "label": "28d",
        "start": (SETTLED - timedelta(days=27)).isoformat(),
        "end": SETTLED.isoformat(),
    }
    assert result["totals"]["clicks"] == 280
    assert result["previous_period"]["clicks"] == 560
    assert result["change"]["clicks"] == -280
    assert result["comparable"] is True


async def test_movers_names_what_moved_and_in_which_direction(site):
    """The tool behind "why did my traffic fall?". An answer that says "some
    pages lost traffic" is not an answer."""
    ids, conn = site
    org, mine = ids["mine"]

    for offset in range(56):
        day = SETTLED - timedelta(days=offset)
        recent = offset < 28
        await add_query(conn, org, mine, day, "sourdough bread",
                        2 if recent else 12, 300, 11.0)
        await add_query(conn, org, mine, day, "rye bread",
                        9 if recent else 3, 200, 9.0)

    run = await run_tool(conn, scope_for(org, mine), "get_movers", {})
    movers = run.result["movers"]

    assert [m["label"] for m in movers] == ["sourdough bread"]
    assert movers[0]["clicks"] == 56 and movers[0]["clicks_before"] == 336
    assert movers[0]["change"] == -280
    assert run.result["compared_with"]["end"] == (
        SETTLED - timedelta(days=28)
    ).isoformat()

    gains = await run_tool(
        conn, scope_for(org, mine), "get_movers", {"direction": "up"}
    )
    assert [m["label"] for m in gains.result["movers"]] == ["rye bread"]


async def test_movers_never_pads_the_list_with_things_that_did_not_move(site):
    ids, conn = site
    org, mine = ids["mine"]
    for offset in range(56):
        await add_query(conn, org, mine, SETTLED - timedelta(days=offset),
                        "steady", 5, 100, 6.0)

    run = await run_tool(conn, scope_for(org, mine), "get_movers", {})
    assert run.result["movers"] == []
    assert "Nothing moved" in run.result["note"]


async def test_a_search_with_no_data_says_so_by_name(site):
    ids, conn = site
    org, mine = ids["mine"]
    run = await run_tool(
        conn, scope_for(org, mine), "get_keyword_history", {"phrase": "artisan rolls"}
    )
    assert run.result["has_data"] is False
    assert "artisan rolls" in run.result["note"]


async def test_a_page_that_does_not_exist_is_not_an_error(site):
    ids, conn = site
    org, mine = ids["mine"]
    run = await run_tool(
        conn, scope_for(org, mine), "get_page_detail", {"url": "/nowhere"}
    )
    assert run.result["found"] is False
    assert "mine.example" in run.result["note"]


async def test_a_page_url_from_another_website_simply_does_not_match(site):
    ids, conn = site
    org, mine = ids["mine"]
    other_org, theirs = ids["theirs"]
    await conn.execute(
        "insert into pages (organization_id, website_id, url, url_hash, path) "
        "values (%s,%s,%s,sha256(%s),%s)",
        (other_org, theirs, "https://theirs.example/secret",
         b"https://theirs.example/secret", "/secret"),
    )

    run = await run_tool(
        conn, scope_for(org, mine),
        "get_page_detail", {"url": "https://theirs.example/secret"},
    )
    assert run.result["found"] is False


async def test_open_issues_arrive_already_ranked(site):
    """Ranking is code. The model is handed the order so it has no reason to
    invent one."""
    ids, conn = site
    org, mine = ids["mine"]
    for type_key, impact in (("missing_title", 5), ("thin_content", 90)):
        await conn.execute(
            "insert into issues (organization_id, website_id, type_key, scope_type,"
            " fingerprint, severity, impact_score, evidence)"
            " values (%s,%s,%s,'page',%s,'high',%s,%s)",
            (org, mine, type_key, uuid.uuid4().hex, impact,
             json.dumps({"url": "https://mine.example/a"})),
        )

    run = await run_tool(conn, scope_for(org, mine), "get_open_issues", {})
    assert [i["type"] for i in run.result["issues"]] == [
        "thin_content", "missing_title"
    ]
    assert run.result["issues"][0]["estimated_monthly_clicks"] == 90
    assert "computed in code" in run.result["ranked_by"]


async def test_site_changes_reports_freshness_alongside_the_changes(site):
    """"Your traffic fell" is a different sentence if the last crawl was
    yesterday than if it was in March."""
    ids, conn = site
    org, mine = ids["mine"]
    await conn.execute(
        "insert into crawls (organization_id, website_id, trigger, status,"
        " finished_at, pages_fetched) values (%s,%s,'manual','completed',%s,42)",
        (org, mine, date.today()),
    )
    await add_totals(conn, org, mine, SETTLED, 10, 100, 5.0)

    run = await run_tool(conn, scope_for(org, mine), "get_site_changes", {})
    freshness = run.result["freshness"]
    assert freshness["pages_crawled"] == 42
    assert freshness["search_data_through"] == SETTLED.isoformat()


async def test_a_tool_run_records_what_it_did(site):
    ids, conn = site
    org, mine = ids["mine"]
    run = await run_tool(
        conn, scope_for(org, mine), "get_top_queries", {"period": "7d"}
    )
    step = run.as_step()
    assert step["tool"] == "get_top_queries"
    assert step["input"] == {"period": "7d"}
    assert step["error"] is None


async def test_a_single_answer_reports_no_row_count_rather_than_zero(site):
    """"0 rows" beside a summary reads as "nothing came back", which is the
    opposite of what happened. A count is only shown where one means
    something."""
    ids, conn = site
    org, mine = ids["mine"]
    await add_totals(conn, org, mine, SETTLED, 10, 100, 5.0)

    summary = await run_tool(conn, scope_for(org, mine), "get_performance_summary", {})
    assert summary.rows is None

    # A list that is genuinely empty still reports zero, because there the
    # zero is the answer: nothing moved.
    movers = await run_tool(conn, scope_for(org, mine), "get_movers", {})
    assert movers.rows == 0
