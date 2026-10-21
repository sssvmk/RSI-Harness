"""The harness's own check of a passage's permissions (design §18.7): a passage is allowed only if it is public or lists the user, and one with no permissions is allowed for nobody."""


def allowed_for(perm: dict | None, user: str) -> bool:
    return bool(perm) and (perm.get("public") is True or user in (perm.get("users") or []))
