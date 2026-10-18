"""
Long-term memory, visible and erasable by the person it's about.

  GET    /api/v1/memory/{user_id}  — what Haven remembers
  DELETE /api/v1/memory/{user_id}  — forget all of it

For a mental-health product, "what do you know about me, and can I delete
it" is a trust requirement, not a settings page.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import memory

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class MemoryResponse(BaseModel):
    facts: list[str]


class ForgetResponse(BaseModel):
    deleted: int


@router.get("/{user_id}", response_model=MemoryResponse)
async def get_memory(user_id: str) -> MemoryResponse:
    return MemoryResponse(facts=await memory.load_facts(user_id))


@router.delete("/{user_id}", response_model=ForgetResponse)
async def forget_memory(user_id: str) -> ForgetResponse:
    return ForgetResponse(deleted=await memory.forget(user_id))
