# core/utils/roles.py
def is_admin_role(request) -> bool:
    role = getattr(getattr(request, "user", None), "role", None)
    if role and str(role).lower() == "admin":
        return True
    if str(request.session.get("role", "")).lower() == "admin":
        return True
    return False
