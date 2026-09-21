# 14 — Disconnect and deletion

The last two items in the V1 definition of done, and the two teams routinely
defer past beta:

- [x] disconnect Google
- [x] delete their account

Both are compliance surface, both were promised in the M0 policy documents
that Google's review reads, and neither is retrofittable without an awkward
conversation. This is the engineering record of what the code actually does,
so the policy text and the behaviour can be checked against each other.

## The cascade is not enough

A naive `delete from organizations` leaves **eleven tenant tables behind**:

```
alert_events          page_snapshots        llm_calls
gsc_query_daily       gsc_page_daily        backlink_changes
gsc_query_page_daily  ga4_page_daily        audit_log
ga4_goal_daily        ga4_dimension_daily
```

The six Google fact tables and `page_snapshots` because **a partitioned table
cannot be the target of a foreign key**; the logs because they were built as
append-only records of what the system did rather than as part of the object
graph; `alert_events` because its link to `alert_rules` is `ON DELETE SET
NULL`, so it survives the cascade as a row pointing at nothing while still
carrying the organisation it belonged to.

Between them those eleven hold every search query, every impression, every
page title and meta description ever fetched. `api/account/deletion.py` names
them in `UNREACHABLE`, and a test derives the same list from the foreign keys
so a table added later cannot quietly join them.

## Three things outside Postgres

| What | Why it needs its own step |
| --- | --- |
| The encrypted refresh token | `secrets.oauth_tokens` is the **parent** of `connections`, so deleting the connection leaves the secret. The one thing a cascade actively cannot help with. |
| The fetched HTML | An object store has no foreign keys. The key layout starts with the website id precisely so a deletion request has a handle on it. |
| The customer's access at Google | Revoked, not merely dropped. Deleting our copy of a refresh token does not stop it working. |

Revocation goes through the Hub (`revoke_organisation`), because only the Hub
may construct a Google client — and through the vault, because exactly one
module is allowed to know how to read `secrets`. Both are enforced by tests
rather than by convention.

**Revocation is best effort.** If Google is unreachable the secret is still
destroyed, because the customer asked us to delete their data and keeping
their refresh token until Google answers the phone is not an option. The
failure is counted, returned, put on the receipt and shown on the confirmation
screen, which tells them to remove our access from their Google security
settings themselves.

## Whose account is it

A person can belong to several organisations. Deleting the person must not
delete a colleague's data, and must not leave an organisation with nobody who
can administer it:

- An organisation where they are the last **owner** is deleted entirely.
- An organisation with another owner keeps going; they are just removed.

"Sole owner", not "only member" — an organisation with a viewer and one owner
still has nobody who could take it over.

## Immediate, and confirmed

No grace period and no soft delete. A soft delete that is never hardened is
exactly the failure the launch-readiness list names: *the data-deletion path
implemented and verified to **actually delete***, and a deletion that quietly
retains everything for thirty days is not the thing the privacy policy
describes.

Two guards instead, both standard for an irreversible action:

- **The caller types their own email address.** The difference between a
  mis-click and a decision, and the one field a hostile page could not supply
  on the customer's behalf.
- **They are told what will go first.** `GET /account` lists the websites and
  connections about to disappear. A dialogue that says "this cannot be undone"
  without saying what "this" is asks somebody to accept a consequence they
  cannot see.

## The sign-in

The application stores no credentials of its own — migration 0001 has no
`password_hash` column, because a table that cannot leak one is strictly safer
than a table that can. So the identity lives with the auth provider and is
deleted through its admin API.

With no admin key configured, every byte of application data still goes and
the response **says the login remains**. That is a worse outcome than deleting
both and a much better one than claiming to have done it — so the confirmation
screen tells the customer to contact support, rather than letting them
discover it at the next sign-in.

## The receipt

Two obligations pull against each other: a customer asks to be erased and we
must erase them; six months later somebody asks whether we did, and "we think
so" is not an answer.

`deletion_receipts` keeps the **shape** of the deletion — when, how many rows
from which tables, how many tokens revoked, whether the sign-in went — and
none of its content. No email, no name, no domain. `user_id` and
`organization_ids` are random UUIDs that no longer resolve to anything; they
identify the event, not the person.

It sits outside the tenancy graph deliberately: no foreign keys, nothing
cascades into it, and it outlives everything it describes. A deletion record
that the deletion deletes is not a record. It is also operator-only — with no
organisation left to scope a policy by, the request-path role gets a policy
that matches nothing rather than a `REVOKE` that `roles.sql` would silently
undo on its next run.

## How it is verified

`test_account_deletion.py` seeds a row in **every** table that carries an
organisation — the realistic path for most, explicit inserts for the expansion
seams nothing writes yet — deletes the account, and asserts that none of them
has anything left.

It also asserts its own coverage: if a table is added later and nothing seeds
it, the "before" assertion fails with its name rather than the test quietly
passing over a table it never touched. That property is the whole point,
because a cascade covers thirty of these and leaves eleven behind.
