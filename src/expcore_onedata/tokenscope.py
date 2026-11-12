# Provenance: new
"""What a token without a data-access caveat reaches, read back from Onezone: its scope, its user's admin privileges
and its user's clusters.

A writing condition's tool-server token carries no data-access caveat (`tokens.tool_server_spec`, #177), so nothing in
the token keeps it to the tool-server plane's spaces. Its reach is its user's: every space the user is an effective
member of, directly or through a group; every Onezone operation an admin privilege of the user allows; and every
provider a cluster of the user lets it act through. Three reads bound that reach, and each refuses what it cannot read
(unknown is not a pass):

1. **The scope equals the plane's spaces.** Onezone's `POST /tokens/infer_access_token_scope` answers, for a user's
   token, `dataAccessScope.spaces` keyed by space id: the user's effective spaces narrowed by the token's data-access
   caveats, so with none of them every effective space (onedata/oz-worker, release/25.0 at
   03af698e5759f9db97a003047eb4e29e4ba7507f, src/logic/token_logic_plugin.erl, `infer_access_token_scope/2`; and
   onedata/ctool, release/25.0 at 019fee12a1638ca516da97a931a2d6138fa25279, src/auth/data_access_caveats.erl,
   `match_available_spaces/2`, whose fold over no caveat returns every space it is given). A scope with a space beyond
   the plane's, a space declared for the data path alone included, or without one of the plane's, is refused.
2. **The user holds no Onezone admin privilege.** `GET /users/{id}/effective_privileges`, read with the admin
   credential, answers the user's admin privileges in Onezone, its own and its groups' (onedata/onezone-swagger,
   release/25.0 at 58c697695fd73ff480f4a6e5fdbf733036465398, paths/users/id/effective_privileges.yaml; the reader needs
   `oz_view_privileges`). The list must be empty. No source sends this call and no live run has made it yet, so it is
   in `client.UNVERIFIED_ENDPOINTS`.
3. **The user is an effective member of no cluster.** A provider's cluster holds its members' privileges over the
   provider. In onedata/oz-worker, release/25.0 at 03af698e5759f9db97a003047eb4e29e4ba7507f, a user with
   `cluster_update` on a provider's cluster may manage and mint that provider's tokens
   (src/logic/token_logic_plugin.erl, `authorize/2`, the `provider_temporary_token` clauses) and update the provider's
   instance, domain configuration and DNS records, and one with `cluster_delete` may delete the provider
   (src/logic/provider_logic_plugin.erl, `authorize/2`). A member of any provider's cluster therefore acts through
   that provider beyond the plane's spaces, whatever the scope and the admin privileges say.
   Onezone's REST description has no read of another user's effective clusters: onedata/onezone-swagger, release/25.0
   at 58c697695fd73ff480f4a6e5fdbf733036465398, has no `effective_clusters` under paths/users/id/, and the oz-worker
   commit above routes none under `/users/:id` (src/http/rest/routes/user_routes.erl). What both have is the current
   user's, `GET /user/effective_clusters`, answering `clusters`, a list of ids, with no specific privilege needed
   (paths/user/effective_clusters.yaml, whose description says it is for the current user only, and the route of
   that path in user_routes.erl). So it is sent as the token being checked, not as the admin: it answers which clusters
   that token acts in, which is what the check is for. The list must be empty. No source sends this call and no live
   run has made it yet, so it is in `client.UNVERIFIED_ENDPOINTS`.

A read that gets no answer raises `ExaminationFailed` with the `PlatformError`, so a caller tells an outage from a
refusal; an answer that cannot be read, and one that differs, raise a `TokenRefused`. A message names a space by its
position (`spaces[i]`), counts the spaces the configuration does not declare, names admin privileges, counts clusters,
and never holds an id, a name or the token.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.tokencheck import ExaminationFailed, TokenRefused

SCOPE_PATH = "/tokens/infer_access_token_scope"
CLUSTERS_PATH = "/user/effective_clusters"  # the current user's, so it is sent as the token being checked
_SHOWN_PRIVILEGES = 8  # the most admin privileges a refusal names


class ScopeMismatch(TokenRefused):
    """A token's scope, as Onezone infers it, is not exactly the spaces of its plane."""


class AdminPrivileged(TokenRefused):
    """A principal whose token has no data-access caveat holds an admin privilege of Onezone."""


class ClusterMember(TokenRefused):
    """A principal whose token has no data-access caveat is an effective member of a cluster."""


def scope_spaces(client: OnezoneClient, token: str) -> set[str]:
    """Return the ids of the spaces Onezone's scope of a token lists; raise as the module says."""
    try:
        answer = client.query(SCOPE_PATH, {"token": token})
    except PlatformError as error:
        raise ExaminationFailed(f"the token's scope could not be read ({error})", error) from None
    scope = answer.get("dataAccessScope") if isinstance(answer, Mapping) else None
    spaces = scope.get("spaces") if isinstance(scope, Mapping) else None
    if not isinstance(spaces, Mapping) or not all(isinstance(key, str) for key in spaces):
        raise TokenRefused("the token's scope answer lists no spaces: it has no dataAccessScope.spaces keyed by id")
    return set(spaces)


def require_scope(found: set[str], wanted: Sequence[str], config: PlatformConfig) -> None:
    """Require the scope's spaces to be the plane's exactly; refuse naming the extra and missing ones by position."""
    positions = {entry.space.id: entry.position for entry in config.spaces}
    extra, missing = found - set(wanted), set(wanted) - found
    if not extra and not missing:
        return
    also = sorted((positions[s] for s in extra if s in positions), key=_order)
    undeclared = sum(1 for s in extra if s not in positions)
    if undeclared:
        also.append(f"{undeclared} space{'' if undeclared == 1 else 's'} the configuration does not declare")
    lacks = sorted((positions.get(s, "an undeclared space") for s in missing), key=_order)
    parts = ([f"has also {', '.join(also)}"] if also else []) + ([f"lacks {', '.join(lacks)}"] if lacks else [])
    raise ScopeMismatch(f"the token's scope differs from the plane's spaces: {'; '.join(parts)}")


def _order(position: str) -> tuple[int, str]:
    """Sort `spaces[i]` by i, and anything else after them."""
    digits = position.removeprefix("spaces[").removesuffix("]")
    return (int(digits), "") if digits.isdigit() else (1 << 30, position)


def admin_privileges(client: OnezoneClient, user_id: str) -> list[str]:
    """Return a user's effective Onezone admin privileges, read with the admin credential; raise as the module says."""
    try:
        answer = client.get(f"/users/{quote(user_id, safe='')}/effective_privileges")
    except PlatformError as error:
        what = "the principal's Onezone admin privileges"
        raise ExaminationFailed(f"{what} could not be read ({error})", error) from None
    privileges = answer.get("privileges") if isinstance(answer, Mapping) else None
    if not isinstance(privileges, list) or not all(isinstance(p, str) for p in privileges):
        raise TokenRefused(
            "the principal's Onezone admin privileges could not be read: 'privileges' is not a list of strings"
        )
    return privileges


def require_no_admin(privileges: Sequence[Any]) -> None:
    """Refuse a principal that holds any Onezone admin privilege, naming them (sorted, the first few)."""
    if not privileges:
        return
    held = sorted({str(p)[:64] for p in privileges})
    more = len(held) - _SHOWN_PRIVILEGES
    shown = ", ".join(held[:_SHOWN_PRIVILEGES]) + (f" and {more} more" if more > 0 else "")
    raise AdminPrivileged(f"the principal holds Onezone admin privileges: {shown}")


def effective_clusters(client: OnezoneClient, token: str) -> list[str]:
    """Return the clusters the token's user is an effective member of, read as that token; raise as the module says."""
    try:
        answer = client.get(CLUSTERS_PATH, token=token)
    except PlatformError as error:
        raise ExaminationFailed(f"the token's clusters could not be read ({error})", error) from None
    clusters = answer.get("clusters") if isinstance(answer, Mapping) else None
    if not isinstance(clusters, list) or not all(isinstance(c, str) for c in clusters):
        raise TokenRefused("the token's clusters could not be read: 'clusters' is not a list of strings")
    return clusters


def require_no_clusters(clusters: Sequence[Any]) -> None:
    """Refuse a principal that is an effective member of any cluster, counting them and never naming one."""
    if clusters:
        count = len(clusters)
        raise ClusterMember(f"the principal is an effective member of {count} cluster{'' if count == 1 else 's'}")
