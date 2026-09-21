"""Deleting an account, and proving it.

The launch-readiness item is worded carefully: *the data-deletion path
implemented and verified to ACTUALLY DELETE*. So the central test here does
not check that the code ran — it seeds a row in every single table that
carries an organisation, deletes the account, and asserts that none of them
has anything left.

It also asserts its own coverage. If a table is added later and nothing here
seeds it, the "before" assertion fails with its name rather than the test
quietly passing over a table it never touched. That is the property that
matters, because a cascade covers thirty of these and leaves eleven behind.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.account.deletion import UNREACHABLE, delete_user, summary_for
from api.account.identity import IdentityOutcome
from api.adapters import db
from api.analysis.catalogue import seed
from api.analysis.runner import AnalysisRunner
from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.runner import CrawlRunner
from api.crawler.storage import NullArtifactStore
from api.tests.conftest import auth_headers
from api.tests.fake_website import ORIGIN, FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website

DAY = date.today() - timedelta(days=5)


async def tenant_tables(conn) -> list[str]:
    """Every table that carries an organisation. Derived, never listed."""
    rows = await (
        await conn.execute(
            """
            select c.relname as t
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind in ('r','p')
               and c.relispartition = false
               and exists(select 1 from information_schema.columns col
                           where col.table_name = c.relname
                             and col.column_name = 'organization_id')
             order by c.relname
            """
        )
    ).fetchall()
    return [row["t"] for row in rows]


async def counts(conn, org) -> dict[str, int]:
    out = {}
    for table in await tenant_tables(conn):
        row = await (
            await conn.execute(
                f"select count(*) as n from {table} where organization_id = %s", (org,)
            )
        ).fetchone()
        out[table] = int(row["n"])
    return out


async def fill_everything(conn, org, website_id, user_id) -> None:
    """One row in every tenant table the product does not write by itself.

    The expansion seams (backlinks, competitors, alerts, content drafts) have
    no code path yet, and the whole point of this test is that a deletion
    reaches tables nobody has thought about lately.
    """
    ids = {name: uuid.uuid4() for name in (
        "alert_rule", "competitor", "keyword", "referring_domain", "backlink",
        "surface", "api_key", "issue", "page",
    )}
    issue_row = await (
        await conn.execute(
            "select id from issues where website_id = %s limit 1", (website_id,)
        )
    ).fetchone()
    recommendation = await (
        await conn.execute(
            "select id from recommendations where website_id = %s limit 1",
            (website_id,),
        )
    ).fetchone()

    statements: list[tuple[str, tuple]] = [
        # The vendor catalogue is shared, not tenant data — seeded here only
        # so the backlink rows have something to point at.
        ("insert into data_providers (key, label) values ('vendor','Vendor')"
         " on conflict (key) do nothing", ()),
        ("insert into organization_branding (organization_id) values (%s)", (org,)),
        ("insert into api_keys (id, organization_id, name, prefix, key_hash)"
         " values (%s,%s,'ci',%s,%s)",
         (ids["api_key"], org, uuid.uuid4().hex[:12], uuid.uuid4().hex)),
        ("insert into alert_rules (id, organization_id, website_id, kind)"
         " values (%s,%s,%s,'traffic_drop')",
         (ids["alert_rule"], org, website_id)),
        ("insert into alert_events (organization_id, website_id, rule_id,"
         " severity, title) values (%s,%s,%s,'high','Traffic fell')",
         (org, website_id, ids["alert_rule"])),
        ("insert into competitors (id, organization_id, website_id, domain)"
         " values (%s,%s,%s,'rival.example')",
         (ids["competitor"], org, website_id)),
        ("insert into keywords (id, organization_id, website_id, phrase,"
         " phrase_hash, source) values (%s,%s,%s,'sourdough',sha256('s'),'gsc')",
         (ids["keyword"], org, website_id)),
        ("insert into referring_domains (id, organization_id, website_id, domain,"
         " provider_key) values (%s,%s,%s,'links.example','vendor')",
         (ids["referring_domain"], org, website_id)),
        ("insert into backlinks (id, organization_id, website_id, source_url,"
         " source_url_hash, target_url, provider_key)"
         " values (%s,%s,%s,'https://links.example/a',sha256('a'),"
         " 'https://example.com/','vendor')",
         (ids["backlink"], org, website_id)),
        ("insert into backlink_changes (organization_id, website_id, backlink_id,"
         " observed_on, change, provider_key)"
         " values (%s,%s,%s,%s,'new','vendor')",
         (org, website_id, ids["backlink"], DAY)),
        ("insert into website_surfaces (organization_id, website_id, surface_key)"
         " values (%s,%s,'organic_search')", (org, website_id)),
        ("insert into external_metrics (organization_id, website_id, subject_kind,"
         " subject_ref, metric, provider_key, as_of, value_numeric)"
         " values (%s,%s,'website',%s,'domain_rating','vendor',%s,42)",
         (org, website_id, str(website_id), DAY)),
        ("insert into psi_samples (organization_id, website_id, url)"
         " values (%s,%s,'https://example.com/')", (org, website_id)),
        ("insert into content_drafts (organization_id, website_id, kind, title)"
         " values (%s,%s,'article','Draft')", (org, website_id)),
        ("insert into sync_runs (organization_id, website_id, provider_key,"
         " service, kind, status)"
         " values (%s,%s,'google','search_console','incremental','succeeded')",
         (org, website_id)),
        ("insert into gsc_query_page_daily (organization_id, website_id, date,"
         " query_hash, query, url_hash, url, clicks, impressions, position)"
         " values (%s,%s,%s,sha256('q'),'q',sha256('u'),'https://example.com/',"
         " 1,10,4.0)", (org, website_id, DAY)),
        ("insert into ga4_dimension_daily (organization_id, website_id, date,"
         " dimension_type, dimension_value, sessions)"
         " values (%s,%s,%s,'session_source','google',5)", (org, website_id, DAY)),
        ("insert into ga4_goal_events (organization_id, website_id, event_name,"
         " label) values (%s,%s,'contact_form_submit','Contact')",
         (org, website_id)),
        ("insert into ga4_goal_daily (organization_id, website_id, date,"
         " event_name, event_count) values (%s,%s,%s,'contact_form_submit',3)",
         (org, website_id, DAY)),
        ("insert into ga4_page_daily (organization_id, website_id, date, url_hash,"
         " page_path, sessions) values (%s,%s,%s,sha256('u'),'/',4)",
         (org, website_id, DAY)),
        ("insert into ga4_daily (organization_id, website_id, date,"
         " channel_group, sessions, active_users)"
         " values (%s,%s,%s,'organic',10,8)", (org, website_id, DAY)),
        ("insert into audit_log (organization_id, actor_kind, actor_user_id,"
         " action, subject_kind) values (%s,'user',%s,'website.created','website')",
         (org, user_id)),
        ("insert into llm_calls (organization_id, website_id, purpose, model,"
         " model_provider, derived_from) values (%s,%s,'issue_explanation','m',"
         " 'anthropic','{\"x\":1}')", (org, website_id)),
        ("insert into actions (organization_id, website_id, issue_id,"
         " recommendation_id, capability, before_state, status)"
         " values (%s,%s,%s,%s,'page.meta.update','{}','applied')",
         (org, website_id, issue_row["id"] if issue_row else None,
          recommendation["id"] if recommendation else None)),
    ]
    for sql, params in statements:
        await conn.execute(sql, params)


@pytest.fixture
async def account(client, two_tenants, service_conn):
    """One organisation with a row in every tenant table, and a neighbour."""
    await seed(service_conn)
    user, org = two_tenants["user_a"], two_tenants["org_a"]
    await service_conn.execute(
        "select app.ensure_partitions_for_backfill(%s, %s)",
        (date.today() - timedelta(days=400), date.today()),
    )

    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = uuid.UUID(created.json()["id"])
    await verify_website(service_conn, org, website_id)

    fake = FakeWebsite(
        pages={
            "/": page("Home", links=("/a",)),
            "/a": page("", description=None),
        },
        sitemap=sitemap_for(["/", "/a"]),
    )
    crawl = await (
        await service_conn.execute(
            "insert into crawls (organization_id, website_id, trigger) "
            "values (%s,%s,'manual') returning id",
            (org, website_id),
        )
    ).fetchone()
    limiter = HostLimiter(default_delay=0.0)
    await CrawlRunner(
        service_conn,
        fetcher=Fetcher(limiter, client=fake.client()),
        store=NullArtifactStore(),
        limiter=limiter,
    ).run(crawl_id=crawl["id"], organization_id=org, website_id=website_id,
          origin=ORIGIN, max_pages=20)

    await service_conn.execute(
        "insert into gsc_totals_daily (organization_id, website_id, date, clicks,"
        " impressions, position) values (%s,%s,%s,10,100,4.0)",
        (org, website_id, DAY),
    )
    await service_conn.execute(
        "insert into gsc_query_daily (organization_id, website_id, date, query_hash,"
        " query, clicks, impressions, position)"
        " values (%s,%s,%s,sha256('q'),'sourdough',5,80,7.0)",
        (org, website_id, DAY),
    )
    await service_conn.execute(
        "insert into gsc_page_daily (organization_id, website_id, date, url_hash,"
        " url, clicks, impressions, position)"
        " values (%s,%s,%s,sha256('u'),'https://example.com/',5,80,7.0)",
        (org, website_id, DAY),
    )
    await AnalysisRunner(service_conn).run(
        organization_id=org, website_id=website_id, crawl_id=crawl["id"]
    )

    # The plan, the report, a conversation and a scheduled run.
    await client.post(
        f"/api/v1/websites/{website_id}/reports", headers=auth_headers(user)
    )
    conversation = await (
        await service_conn.execute(
            "insert into conversations (organization_id, website_id, created_by,"
            " title) values (%s,%s,%s,'why?') returning id",
            (org, website_id, user),
        )
    ).fetchone()
    await service_conn.execute(
        "insert into conversation_messages (organization_id, conversation_id, seq,"
        " role, content) values (%s,%s,1,'user','why?')",
        (org, conversation["id"]),
    )
    await service_conn.execute(
        "insert into scheduled_runs (organization_id, website_id, job, window_start)"
        " values (%s,%s,'crawl_website', now())",
        (org, website_id),
    )
    await fill_everything(service_conn, org, website_id, user)
    return org, website_id, user, service_conn


class FakeIdentity:
    def __init__(self, outcome: IdentityOutcome) -> None:
        self.outcome = outcome
        self.deleted: list[uuid.UUID] = []

    async def delete(self, user_id):
        self.deleted.append(user_id)
        return self.outcome


# -- the central claim ------------------------------------------------------
async def test_every_table_that_holds_the_customer_is_emptied(account):
    """The launch-readiness item, verified rather than asserted.

    A cascade reaches thirty of these tables and leaves eleven behind — the
    six partitioned Google fact tables, page_snapshots and the append-only
    logs — because a partitioned table cannot be the target of a foreign key.
    Those eleven hold every search query, impression, page title and meta
    description we ever stored.
    """
    org, website_id, user, conn = account

    before = await counts(conn, org)
    empty = sorted(table for table, n in before.items() if n == 0)
    assert empty == [], (
        "this test does not cover these tables, so it proves nothing about "
        f"them: {empty}"
    )

    await delete_user(conn, user, store=NullArtifactStore(),
                      revoke=False, identity=FakeIdentity(IdentityOutcome(True)))

    after = await counts(conn, org)
    left = {table: n for table, n in after.items() if n}
    assert left == {}, f"the deletion left data behind in: {sorted(left)}"


async def test_the_unreachable_list_is_the_one_the_schema_says_it_is(account):
    """Derived from the foreign keys, so a table added later with no cascade
    cannot quietly join the list of things a deletion misses."""
    org, _, _, conn = account

    rows = await (
        await conn.execute(
            """
            with recursive reachable(t) as (
                select 'organizations'::text
                union
                select fk.conrelid::regclass::text
                  from pg_constraint fk join reachable r
                    on fk.confrelid::regclass::text = r.t
                 where fk.contype = 'f' and fk.confdeltype = 'c'
            )
            select c.relname as t
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind in ('r','p')
               and c.relispartition = false
               and exists(select 1 from information_schema.columns col
                           where col.table_name = c.relname
                             and col.column_name = 'organization_id')
               and c.relname not in (select t from reachable)
             order by 1
            """
        )
    ).fetchall()
    assert sorted(row["t"] for row in rows) == sorted(UNREACHABLE)


async def test_the_neighbour_is_untouched(account, two_tenants, client):
    """The obvious thing to get wrong when a query spans organisations."""
    org, _, user, conn = account
    other_org, other_user = two_tenants["org_b"], two_tenants["user_b"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://neighbour.example"},
        headers=auth_headers(other_user),
    )
    assert created.status_code == 201

    await delete_user(conn, user, store=NullArtifactStore(), revoke=False)

    still = await (
        await conn.execute(
            "select count(*) as n from websites where organization_id = %s",
            (other_org,),
        )
    ).fetchone()
    assert still["n"] == 1


# -- the secrets ------------------------------------------------------------
async def test_the_encrypted_refresh_token_goes_too(account):
    """The vault row is the PARENT of the connection, so deleting the
    connection leaves the encrypted refresh token sitting in `secrets`
    forever. It is the one thing a cascade actively cannot help with."""
    org, website_id, user, conn = account

    token = await (
        await conn.execute(
            "insert into secrets.oauth_tokens (ciphertext, wrapped_dek, nonce)"
            " values ('x','y','z') returning id"
        )
    ).fetchone()
    await conn.execute(
        "update connections set refresh_token_id = %s where organization_id = %s",
        (token["id"], org),
    )

    await delete_user(conn, user, store=NullArtifactStore(), revoke=False)

    left = await (
        await conn.execute(
            "select count(*) as n from secrets.oauth_tokens where id = %s",
            (token["id"],),
        )
    ).fetchone()
    assert left["n"] == 0


async def test_the_fetched_html_goes_too(account):
    """An object store has no foreign keys. The key layout starts with the
    website id precisely so a deletion request has a handle on it."""
    org, website_id, user, conn = account
    store = NullArtifactStore()
    await store.put(f"{website_id}/{uuid.uuid4()}/abc.html.gz", b"")
    await store.put(f"reports/{website_id}/2026-09-21.html.gz", b"")
    await store.put("other-website/keep.html.gz", b"")

    result = await delete_user(conn, user, store=store, revoke=False)

    assert result.objects == 2
    assert store.keys == ["other-website/keep.html.gz"]


# -- whose account is it ----------------------------------------------------
async def test_an_organisation_with_another_owner_survives(
    account, two_tenants, service_conn
):
    """Deleting a person must not delete a colleague's data — and must not
    leave an organisation with nobody who can administer it."""
    org, website_id, user, conn = account
    colleague = uuid.uuid4()
    await conn.execute(
        "insert into users (id, email) values (%s,%s)",
        (colleague, f"{uuid.uuid4().hex[:8]}@example.com"),
    )
    await conn.execute(
        "insert into organization_members (organization_id, user_id, role)"
        " values (%s,%s,'owner')",
        (org, colleague),
    )

    result = await delete_user(conn, user, store=NullArtifactStore(), revoke=False)

    assert result.organizations == []
    assert result.organizations_left == [org]
    survived = await (
        await conn.execute(
            "select count(*) as n from websites where organization_id = %s", (org,)
        )
    ).fetchone()
    assert survived["n"] == 1

    gone = await (
        await conn.execute(
            "select count(*) as n from organization_members"
            " where organization_id = %s and user_id = %s",
            (org, user),
        )
    ).fetchone()
    assert gone["n"] == 0


async def test_a_viewer_does_not_count_as_a_successor(account):
    """"Sole owner", not "only member": an organisation with a viewer and one
    owner still has nobody who could take it over."""
    org, website_id, user, conn = account
    viewer = uuid.uuid4()
    await conn.execute(
        "insert into users (id, email) values (%s,%s)",
        (viewer, f"{uuid.uuid4().hex[:8]}@example.com"),
    )
    await conn.execute(
        "insert into organization_members (organization_id, user_id, role)"
        " values (%s,%s,'viewer')",
        (org, viewer),
    )

    result = await delete_user(conn, user, store=NullArtifactStore(), revoke=False)
    assert result.organizations == [org]


# -- the receipt ------------------------------------------------------------
async def test_the_receipt_records_the_shape_and_none_of_the_content(account):
    """A customer asks to be erased and we must erase them; six months later
    somebody asks whether we did, and "we think so" is not an answer."""
    org, website_id, user, conn = account

    result = await delete_user(
        conn, user, store=NullArtifactStore(), revoke=False,
        identity=FakeIdentity(IdentityOutcome(True)),
    )

    row = await (
        await conn.execute(
            "select * from deletion_receipts where id = %s", (result.receipt_id,)
        )
    ).fetchone()
    assert row["user_id"] == user
    assert row["organization_ids"] == [org]
    assert row["rows_deleted"]["gsc_query_daily"] >= 1
    assert row["identity_deleted"] is True
    assert row["completed_at"] is not None
    # Nothing in it names anybody.
    assert "example.com" not in str(row)
    assert "@" not in str(row)


async def test_the_receipt_outlives_what_it_describes(account):
    org, website_id, user, conn = account
    result = await delete_user(conn, user, store=NullArtifactStore(), revoke=False)

    still = await (
        await conn.execute(
            "select count(*) as n from deletion_receipts where id = %s",
            (result.receipt_id,),
        )
    ).fetchone()
    assert still["n"] == 1


async def test_a_customer_cannot_read_the_receipts(client, account):
    """Operator-only. There is no organisation left to scope a policy by, so
    the request-path role gets a policy that matches nothing."""
    org, website_id, user, conn = account
    await delete_user(conn, user, store=NullArtifactStore(), revoke=False)

    async with db.session() as request_path:
        row = await (
            await request_path.execute("select count(*) as n from deletion_receipts")
        ).fetchone()
    assert row["n"] == 0


# -- the sign-in ------------------------------------------------------------
async def test_the_sign_in_is_deleted_too(account):
    org, website_id, user, conn = account
    identity = FakeIdentity(IdentityOutcome(True))

    result = await delete_user(
        conn, user, store=NullArtifactStore(), revoke=False, identity=identity
    )

    assert identity.deleted == [user]
    assert result.identity_deleted is True


async def test_a_sign_in_we_could_not_delete_is_reported_not_swallowed(account):
    """"Your data is gone but your sign-in still works" is something a
    customer needs to be told, not to discover."""
    org, website_id, user, conn = account
    identity = FakeIdentity(IdentityOutcome(False, reason="Supabase returned 500"))

    result = await delete_user(
        conn, user, store=NullArtifactStore(), revoke=False, identity=identity
    )

    assert result.identity_deleted is False
    assert result.identity_reason == "Supabase returned 500"
    # And the data still went.
    assert result.rows["users"] == 1


# -- the API ----------------------------------------------------------------
async def test_the_confirmation_must_match_the_caller_s_own_email(client, account):
    org, website_id, user, conn = account

    response = await client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirm_email": "someone-else@example.com"},
        headers=auth_headers(user, "user@example.com"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_mismatch"

    still = await (
        await conn.execute(
            "select count(*) as n from websites where organization_id = %s", (org,)
        )
    ).fetchone()
    assert still["n"] == 1, "a mismatched confirmation deleted data"


async def test_the_account_screen_says_what_will_go(client, account):
    org, website_id, user, conn = account

    body = (
        await client.get("/api/v1/account", headers=auth_headers(user))
    ).json()
    assert body["websites"] == ["example.com"]
    assert body["organizations_deleted"] == 1
    assert body["organizations_left"] == 0
    assert body["google_connections"] >= 1


async def test_deleting_through_the_api_actually_deletes(client, account):
    org, website_id, user, conn = account

    response = await client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirm_email": "USER@Example.com "},
        headers=auth_headers(user, "user@example.com"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["organizations_deleted"] == 1
    assert body["rows_deleted"] > 0
    # No identity provider configured in the test environment, and it says so
    # rather than claiming the sign-in is gone.
    assert body["sign_in_deleted"] is False
    assert body["sign_in_note"]

    left = await counts(conn, org)
    assert {t: n for t, n in left.items() if n} == {}


async def test_the_summary_is_scoped_to_the_caller(account, two_tenants, conn=None):
    org, website_id, user, service = account
    other = two_tenants["user_b"]

    summary = await summary_for(service, other)
    assert summary["websites"] == []
    assert summary["organizations_deleted"] == 1
