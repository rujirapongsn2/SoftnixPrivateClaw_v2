"""Which groups a group-visibility item (knowledge base, blueprint) is shared with.

The list is explicit and chosen by the owner, like a skill's shared group: being a member of a
group never exposes an item to that group by itself, so joining a group cannot widen sharing.
"""

from collections.abc import Sequence

from fastapi import HTTPException


async def resolve_share_groups(
    state, owner_id: str, requested: Sequence[str] | None, existing: Sequence[str] = ()
) -> list[str]:
    """The groups to share with, validated.

    * `requested` given: used as is (blank/duplicate ids dropped); must be non-empty and every
      id must be a real group. The owner may share with groups they are not in.
    * not given: keep `existing` if there is one; otherwise an owner in exactly one group gets
      that group, and an owner in several (or none) must choose.
    """
    known = {g.id for g in await state.groups.list()}
    if requested is not None:
        wanted = list(dict.fromkeys(g for g in requested if g))
        if not wanted:
            raise HTTPException(status_code=422, detail="Choose at least one group to share with")
        if any(g not in known for g in wanted):
            raise HTTPException(status_code=404, detail="group not found")
        return wanted
    kept = [g for g in existing if g in known]
    if kept:
        return kept
    mine = await state.users.group_ids_for(owner_id)
    if len(mine) == 1:
        return mine
    if not mine:
        raise HTTPException(status_code=400, detail="Join a group before sharing with a group")
    raise HTTPException(status_code=422, detail="Choose which of your groups to share with")
