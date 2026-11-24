"""Simulated sign-in and server-side permissions (prompt §2). Every request carries X-User-Id; a request the
persona's screen level does not allow returns 403 and is logged as PERMISSION_DENIED."""
from .util import AppError, PermissionDenied


def current_user(cfg, request):
    uid = request.headers.get("x-user-id") or ""
    u = cfg.user(uid)
    if not u:
        raise AppError("Sign in first (unknown user %r)" % uid)
    return u


def require(app_state, request, screen, levels=("V", "A"), what=None):
    cfg = app_state.cfg
    u = current_user(cfg, request)
    lvl = cfg.level(u["persona"], screen)
    if lvl not in levels:
        ds = request.headers.get("x-dataset-id") or None
        sc = request.headers.get("x-scenario-id") or None
        app_state.engine.log(ds, sc, None, u["user_id"], "PERMISSION_DENIED", "screen", screen, None, None,
                             {"attempted": what or "open %s" % screen, "level": lvl, "path": request.url.path})
        raise PermissionDenied("%s may not %s" % (u["persona"], what or "open %s" % screen))
    return u
