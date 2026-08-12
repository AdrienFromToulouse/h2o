"""Asking the platform a question.

ADR-001's shape, kept: the model composes prose from one deterministic
retrieval, and it has no tool that could write anything. The gap a miss creates
is written from inside the resolver, three callers deep, where the model cannot
reach it.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from h2o_api import chat

router = APIRouter(tags=["chat"])


class Question(BaseModel):
    question: str = Field(min_length=1)
    #: Threaded onto the gap evidence so a curator can see that a person asked,
    #: not just that a document said. Never shown to the model.
    session_id: str = ""


@router.post("/chat")
def ask(body: Question) -> dict[str, Any]:
    return chat.ask(body.question, session_id=body.session_id)
