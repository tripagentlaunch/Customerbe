"""Wraps the site_saved_items table (My Year calendar) — formerly read and
written straight from the browser with the Supabase anon key + RLS
(member_id = auth.uid()). The service-role client used here bypasses RLS
entirely, so every query re-implements that same "only your own rows"
scoping explicitly via .eq("member_id", member_id) on update/delete —
dropping that filter would let any signed-in member mutate another
member's saved items by guessing an id."""

from typing import Any, Dict, List, Optional

from app.dependencies.supabase_client import get_supabase_admin_client


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")
    return client


def list_items(member_id: str) -> List[dict]:
    client = _require_client()
    return (
        client.table("site_saved_items")
        .select("*")
        .eq("member_id", member_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )


def create_item(member_id: str, fields: Dict[str, Any]) -> dict:
    client = _require_client()
    inserted = client.table("site_saved_items").insert({"member_id": member_id, **fields}).execute().data
    if not inserted:
        raise RuntimeError("insert returned no row")
    return inserted[0]


def update_item(member_id: str, item_id: str, fields: Dict[str, Any]) -> Optional[dict]:
    client = _require_client()
    if not fields:
        rows = (
            client.table("site_saved_items")
            .select("*")
            .eq("id", item_id)
            .eq("member_id", member_id)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None
    updated = (
        client.table("site_saved_items")
        .update(fields)
        .eq("id", item_id)
        .eq("member_id", member_id)  # ownership check — see module docstring
        .execute()
        .data
    )
    return updated[0] if updated else None


def delete_item(member_id: str, item_id: str) -> bool:
    client = _require_client()
    deleted = (
        client.table("site_saved_items")
        .delete()
        .eq("id", item_id)
        .eq("member_id", member_id)  # ownership check — see module docstring
        .execute()
        .data
    )
    return bool(deleted)