"""Issue explanations: the cache, the budget, and the refusal.

The expensive claim in docs/09-mvp-sequence.md is that a 500-page crawl costs
under $0.20 in explanations after cache warm-up. That only holds if the cache
unit is the PROBLEM rather than the page — so the test that a site with two
hundred missing titles makes one call is the milestone's cost test, not a
micro-optimisation check.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.ai.budget import budget_for
from api.ai.cache import cache_key
from api.ai.explain import ExplanationService
from api.ai.metering import reusing
from api.ai.prompts import issue_explanation
from api.ai.providers import ProviderError
from api.analysis.catalogue import seed
from api.tests.fake_model import FakeProvider

GOOD = {
    "what": "Some of your pages have no title.",
    "why": "The title is what people click in Google.",
    "how": ["Write a title of 30 to 60 characters."],
    "effort": "low",
}


@pytest.fixture
async def org(service_conn, two_tenants):
    await seed(service_conn)
    # The explanation cache is global by design — a generation for "missing
    # title" is shared by every website that has one. That makes it shared
    # state between tests too, so each test starts from an empty one rather
    # than inheriting whatever the previous test generated.
    await service_conn.execute("delete from issue_explanations")
    return two_tenants["org_a"], two_tenants["org_b"], service_conn


def service(conn, organization_id, provider=None) -> ExplanationService:
    # Metering goes through the service role in production (llm_calls has no
    # write policy). These tests already hold a service connection, so they
    # lend it rather than making the metering open a second one.
    return ExplanationService(
        conn,
        organization_id=organization_id,
        provider=provider,
        meter=reusing(conn),
    )


async def calls(conn, organization_id):
    rows = await (
        await conn.execute(
            "select purpose, status, cache_hit, model, cost_usd, prompt_version, "
            "       derived_from from llm_calls where organization_id = %s "
            " order by id",
            (organization_id,),
        )
    ).fetchall()
    return rows


# -- with no model at all ---------------------------------------------------
async def test_without_a_provider_the_customer_gets_hand_written_advice(org):
    """Not a degraded mode anyone should be embarrassed by: plain, correct
    advice out of the issue catalogue."""
    org_a, _, conn = org
    result = await service(conn, org_a).explain("missing_title", {"url": "x"})

    assert result.source == "template"
    assert result.reason == "no_provider"
    assert result.what == "Missing page title"
    assert result.how[0].startswith("Write a title")
    # No model ran, so nothing is metered.
    assert await calls(conn, org_a) == []


def test_the_templates_obey_the_same_rule_as_the_model():
    """Rule 1 is not an instruction to the model, it is a property of the
    product. A hand-written fallback quoting "roughly 150 characters" for a
    length we never measure is the same fabrication, just ours."""
    from api.ai.numbers import unsupported_figures
    from api.analysis.catalogue import CATALOGUE

    offenders = {}
    for issue_type in CATALOGUE:
        payload = issue_explanation.payload_for(issue_type.key, {})
        invented = unsupported_figures(
            issue_explanation.template(issue_type.key), payload
        )
        if invented:
            offenders[issue_type.key] = invented
    assert offenders == {}


async def test_every_issue_type_has_hand_written_steps(org):
    """A generic 'open the page and fix it' is what a customer sees when the
    budget runs out, so every type in the catalogue earns real steps."""
    from api.analysis.catalogue import CATALOGUE

    generic = issue_explanation.GENERIC_FIX
    assert [t.key for t in CATALOGUE
            if issue_explanation.TEMPLATE_FIXES.get(t.key, generic) is generic] == []


# -- the cache --------------------------------------------------------------
async def test_the_same_problem_on_two_hundred_pages_is_one_generation(org):
    """The cost test. The cache unit is the problem, not the page."""
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD)
    explainer = service(conn, org_a, provider)

    for index in range(200):
        await explainer.explain(
            "missing_title", {"url": f"https://example.com/page-{index}"}
        )

    assert len(provider.calls) == 1


async def test_the_generation_survives_into_the_next_run(org):
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD)
    first = await service(conn, org_a, provider).explain("missing_title", {})
    assert first.source == "model"

    # A fresh service: the memo is gone, so this can only come from the table.
    second = await service(conn, org_a, provider).explain("missing_title", {})
    assert second.source == "cache"
    assert second.what == GOOD["what"]
    assert len(provider.calls) == 1


async def test_two_organisations_with_the_same_problem_share_one_generation(org):
    """Safe precisely because the key is the whole prompt: a collision means
    the text each would have been shown is identical."""
    org_a, org_b, conn = org
    provider = FakeProvider(responses=GOOD)

    await service(conn, org_a, provider).explain("missing_title", {})
    result = await service(conn, org_b, provider).explain("missing_title", {})

    assert result.source == "cache"
    assert len(provider.calls) == 1


async def test_an_existing_generation_is_used_even_with_no_provider(org):
    """The cache is consulted before the provider is, not after. A site that
    arrives after somebody else's crawl has already explained this problem
    gets the good explanation, not the fallback."""
    org_a, org_b, conn = org
    await service(conn, org_a, FakeProvider(responses=GOOD)).explain(
        "missing_title", {}
    )

    result = await service(conn, org_b).explain("missing_title", {})
    assert result.source == "cache"
    assert result.what == GOOD["what"]


async def test_the_prompt_never_contains_a_customer_url(org):
    """Both a privacy property and the reason the cache works at all. A URL in
    the payload would make every page its own cache entry."""
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD)
    await service(conn, org_a, provider).explain(
        "missing_title", {"url": "https://acme.example/secret-page", "title": "X"}
    )

    sent = provider.calls[0].prompt
    assert "acme.example" not in sent
    assert "secret-page" not in sent


async def test_a_facet_that_changes_the_advice_changes_the_key(org):
    """A title that is too long and one that is too short are different
    problems and must not share an explanation."""
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD)
    explainer = service(conn, org_a, provider)

    await explainer.explain("title_length", {"length": 12})
    await explainer.explain("title_length", {"length": 94})

    assert len(provider.calls) == 2
    directions = {call.payload["facts"]["direction"] for call in provider.calls}
    assert directions == {"too short", "too long"}


def test_anything_in_the_prompt_is_in_the_key():
    """Structural, not remembered: the prompt IS the canonicalised payload, so
    a field cannot enter one without entering the other."""
    payload = issue_explanation.payload_for("title_length", {"length": 12})
    key = cache_key(issue_explanation.VERSION, payload)

    for field in payload:
        mutated = dict(payload)
        mutated[field] = "changed"
        assert cache_key(issue_explanation.VERSION, mutated) != key

    assert cache_key("issue_explanation.v2", payload) != key


# -- refusal ----------------------------------------------------------------
async def test_an_invented_figure_is_refused_and_never_cached(org):
    """The poisoned generation. Nothing about it reaches the customer, and
    nothing about it reaches the cache to be served to somebody else."""
    org_a, _, conn = org
    poisoned = dict(GOOD, why="Pages like this get about 2,400 searches a month.")
    provider = FakeProvider(responses=poisoned)

    result = await service(conn, org_a, provider).explain("missing_title", {})

    assert result.source == "template"
    assert result.reason == "unsupported_numbers"
    assert "2,400" not in result.why

    metered = await calls(conn, org_a)
    assert [row["status"] for row in metered] == ["refused"]
    # We were billed for the call, so it is recorded as costing money.
    assert metered[0]["cost_usd"] == Decimal("0.001")

    cached = await (
        await conn.execute("select count(*) as n from issue_explanations")
    ).fetchone()
    assert cached["n"] == 0


async def test_a_malformed_generation_falls_back_too(org):
    org_a, _, conn = org
    provider = FakeProvider(responses={"what": "x", "effort": "colossal"})

    result = await service(conn, org_a, provider).explain("missing_title", {})
    assert (result.source, result.reason) == ("template", "validation_failed")


async def test_a_provider_failure_is_recorded_and_falls_back(org):
    org_a, _, conn = org
    provider = FakeProvider(error=ProviderError("connection reset"))

    result = await service(conn, org_a, provider).explain("missing_title", {})

    assert (result.source, result.reason) == ("template", "provider_error")
    assert [row["status"] for row in await calls(conn, org_a)] == ["error"]


# -- budget -----------------------------------------------------------------
async def test_bulk_explanations_stop_when_the_allowance_is_spent(org):
    """docs/06-ai-layer.md: over budget, bulk explanations fall back to
    templates. The check is at admission — a limit enforced after the response
    arrives is a report, not a limit."""
    org_a, _, conn = org
    await conn.execute(
        "update organizations set monthly_ai_budget_usd = 1.00 where id = %s",
        (org_a,),
    )
    await conn.execute(
        "insert into llm_calls (organization_id, purpose, model, model_provider,"
        "  cost_usd, derived_from) values (%s,'issue_explanation','m','anthropic',"
        "  1.50, '{\"x\":1}')",
        (org_a,),
    )

    provider = FakeProvider(responses=GOOD)
    result = await service(conn, org_a, provider).explain("missing_title", {})

    assert (result.source, result.reason) == ("template", "budget_exhausted")
    assert provider.calls == []


async def test_an_unpriced_call_is_visible_rather_than_free(org):
    """A budget that treats unpriced calls as free stops working the moment
    somebody adds a model, and nobody notices until the invoice."""
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD, cost=None, model="some-future-model")
    await service(conn, org_a, provider).explain("missing_title", {})

    budget = await budget_for(conn, org_a)
    assert budget.unpriced_calls == 1
    assert budget.spent_usd == Decimal(0)


async def test_a_cache_hit_is_recorded_as_costing_zero_not_unknown(org):
    org_a, _, conn = org
    provider = FakeProvider(responses=GOOD)
    await service(conn, org_a, provider).explain("missing_title", {})
    await service(conn, org_a, provider).explain("missing_title", {})

    budget = await budget_for(conn, org_a)
    assert budget.unpriced_calls == 0
    assert budget.spent_usd == Decimal("0.001")

    metered = await calls(conn, org_a)
    assert [row["cache_hit"] for row in metered] == [False, True]


async def test_a_five_hundred_page_crawl_costs_pennies_in_explanations(org):
    """The milestone's cost criterion, measured rather than asserted.

    A 500-page crawl raises findings across the whole catalogue, many of them
    hundreds of times. What it must NOT do is pay per finding — so this runs
    every issue type, plus the facet variants that legitimately split a type
    into more than one explanation, at real cheap-tier prices and checks the
    metered total.
    """
    from api.ai.pricing import cost_usd
    from api.ai.providers import Generation
    from api.analysis.catalogue import CATALOGUE

    org_a, _, conn = org
    model = "claude-haiku-4-5-20251001"

    class PricedProvider(FakeProvider):
        async def generate(self, **kwargs):
            generation = await super().generate(**kwargs)
            # Representative of the real prompt: a small payload in, three
            # short fields out.
            return Generation(
                data=generation.data, model=model, model_provider="anthropic",
                input_tokens=900, output_tokens=350, cached_input_tokens=0,
                latency_ms=500,
                cost_usd=cost_usd(model, input_tokens=900, output_tokens=350),
            )

    # Numberless prose: this test is about cost, and a canned response that
    # quotes a title length would be correctly refused for the twenty types
    # that have nothing to do with titles.
    provider = PricedProvider(
        responses={
            "what": "Something on your site needs attention.",
            "why": "It affects how you appear in search.",
            "how": ["Open the affected page and correct it."],
            "effort": "low",
        }
    )
    explainer = service(conn, org_a, provider)

    # Every page in a 500-page crawl, asked about every problem it could have.
    for _ in range(500):
        for issue_type in CATALOGUE:
            await explainer.explain(issue_type.key, {"length": 12, "position": 14.2})

    spent = (await budget_for(conn, org_a)).spent_usd
    assert len(provider.calls) == len(CATALOGUE), "one generation per problem kind"
    assert spent < Decimal("0.20"), f"explanations cost {spent} for one crawl"

    # And the next crawl, from the cache, costs nothing at all.
    before = spent
    second = service(conn, org_a, provider)
    for issue_type in CATALOGUE:
        await second.explain(issue_type.key, {"length": 12, "position": 14.2})
    assert (await budget_for(conn, org_a)).spent_usd == before
