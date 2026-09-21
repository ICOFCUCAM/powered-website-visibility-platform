"""The AI Strategist (V1 spec §23, docs/06-ai-layer.md).

One streaming endpoint and three flat ones. The streaming endpoint is the only
place in the API that holds a database connection open for the length of a
model call, and it opens that connection itself rather than taking the request
dependency: FastAPI closes a `yield` dependency before the response body is
streamed, so a handler that relied on `ConnectionDep` here would find its
connection already returned to the pool halfway through the first tool call.

It is still the RLS-bound request role, bound to the same user. Nothing about
streaming relaxes tenancy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.adapters import db
from api.adapters.db import fetch_all, fetch_one
from api.ai.deps import get_chat_provider
from api.ai.prompts.strategist import SUGGESTED_QUESTIONS
from api.ai.strategist import MAX_QUESTION_CHARS, Strategist, StrategistUnavailable
from api.ai.tools import StrategistScope
from api.deps import ConnectionDep, PrincipalDep, WebsiteScopeDep
from api.domain.errors import AppError, NotFound

logger = logging.getLogger("visibility_hub.ai")

router = APIRouter(prefix="/websites", tags=["strategist"])


class AssistantUnavailable(AppError):
    code = "assistant_unavailable"
    status = 503
    message = "The assistant isn't switched on for this installation."
    retriable = False


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    conversation_id: UUID | None = None


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


@router.post("/{website_id}/ai/chat")
async def chat(
    scope: WebsiteScopeDep, principal: PrincipalDep, body: Question
) -> StreamingResponse:
    provider = get_chat_provider()
    if provider is None:
        raise AssistantUnavailable()

    website_id = scope.website.id
    organization_id = scope.website.organization_id
    domain = scope.website.domain
    user_id = principal.user_id

    async def events() -> AsyncIterator[bytes]:
        try:
            async with db.session(user_id) as conn:
                strategist = Strategist(
                    conn,
                    scope=StrategistScope(
                        organization_id=organization_id,
                        website_id=website_id,
                        domain=domain,
                    ),
                    provider=provider,
                    user_id=user_id,
                )
                async for event in strategist.ask(
                    conversation_id=body.conversation_id, question=body.question
                ):
                    yield _sse(event)
        except StrategistUnavailable:
            yield _sse(
                {
                    "type": "error",
                    "code": "assistant_unavailable",
                    "message": AssistantUnavailable.message,
                }
            )
        except LookupError:
            yield _sse(
                {
                    "type": "error",
                    "code": "not_found",
                    "message": "We couldn't find that conversation.",
                }
            )
        except Exception:
            # The status line went out with the first byte, so an exception
            # here cannot become a 500 — it has to be an event, or the browser
            # sees a stream that simply stops.
            logger.exception("strategist stream failed")
            yield _sse(
                {
                    "type": "error",
                    "code": "assistant_unavailable",
                    "message": "Something went wrong mid-answer. Please try again.",
                }
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Proxies that buffer a response defeat the point of streaming it.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{website_id}/conversations")
async def list_conversations(
    scope: WebsiteScopeDep, conn: ConnectionDep
) -> dict[str, Any]:
    rows = await fetch_all(
        conn,
        """
        select id, title, message_count, created_at, last_message_at
          from conversations where website_id = %s
         order by last_message_at desc limit 50
        """,
        (scope.website.id,),
    )
    return {
        "available": get_chat_provider() is not None,
        # The screen opens with these rather than a blank box: a chat that
        # asks a non-technical owner to think of a good question usually gets
        # no question at all.
        "suggested_questions": list(SUGGESTED_QUESTIONS),
        "conversations": [
            {
                "id": str(row["id"]),
                "title": row["title"],
                "message_count": row["message_count"],
                "created_at": row["created_at"],
                "last_message_at": row["last_message_at"],
            }
            for row in rows
        ],
    }


@router.get("/{website_id}/conversations/{conversation_id}")
async def read_conversation(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    conversation_id: Annotated[UUID, Path()],
) -> dict[str, Any]:
    conversation = await fetch_one(
        conn,
        "select id, title, created_at from conversations "
        " where id = %s and website_id = %s",
        (conversation_id, scope.website.id),
    )
    if conversation is None:
        raise NotFound("We couldn't find that conversation.")

    rows = await fetch_all(
        conn,
        """
        select seq, role, content, steps, model, stop_reason, created_at
          from conversation_messages
         where conversation_id = %s order by seq
        """,
        (conversation_id,),
    )
    return {
        "id": str(conversation["id"]),
        "title": conversation["title"],
        "created_at": conversation["created_at"],
        "messages": [
            {
                "seq": row["seq"],
                "role": row["role"],
                "content": row["content"],
                # The audit trail, surfaced rather than hidden: which tools
                # ran, over which window. A disputed sentence is traceable.
                "steps": row["steps"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }


@router.delete("/{website_id}/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    conversation_id: Annotated[UUID, Path()],
) -> None:
    scope.require_write()
    row = await fetch_one(
        conn,
        "delete from conversations where id = %s and website_id = %s returning id",
        (conversation_id, scope.website.id),
    )
    if row is None:
        raise NotFound("We couldn't find that conversation.")
