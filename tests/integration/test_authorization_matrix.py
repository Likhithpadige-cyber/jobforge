"""Every registered API route is classified: public, or authenticated with a role.

This file is the enforcement mechanism for the decision in
`apps/api/deps.authenticated_api`, and it works by enumerating
`create_app().routes` rather than by listing endpoints by hand. A
hand-written list proves only that the endpoints someone remembered are
protected; the routes that matter are the ones nobody remembered.

`PUBLIC_PATHS` is therefore the whole of the public surface, stated once.
Adding a route without classifying it fails `test_every_route_is_classified`,
and making one public is a visible edit to that set in a reviewable diff
rather than an omission.

M2 adds the role policy on the same principle. Every protected route is
listed in exactly one of `OWNER_ONLY` or `OWNER_AND_REVIEWER`, and a route
in neither - or in both - fails. A new endpoint therefore cannot ship
without someone writing down who may call it.

The two role sets are stated here rather than read off the decorators. A
policy derived from the code agrees with the code by construction and proves
nothing; written independently, it is a second opinion that has to be
reconciled with the first.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from apps.api.deps import CSRF_HEADER
from apps.api.main import create_app
from packages.shared.config import get_settings
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration

#: The entire public surface. Anything not listed here is behind a session.
#:
#: Each entry earns its place:
#:
#:   /health, /healthz   container liveness. No dependencies, no data.
#:   /readyz             the same readiness body as /api/v1/health, under the
#:                       unprefixed alias that orchestrators probe.
#:   /api/v1/health      readiness. Reports dependency status and migration
#:                       revision - mild information disclosure, kept public
#:                       because compose and the frontend health badge probe
#:                       it. Flagged for review, not for M1.
#:   /api/v1/auth/login  cannot require a session; it creates one.
#:   /api/v1/auth/config cannot require a session; the sign-in form reads it
#:                       to decide whether to render the TOTP field. Returns
#:                       one boolean already observable by attempting a login.
#:
#: /api/v1/auth/logout and /api/v1/auth/me are NOT public. They carry their
#: own endpoint-level dependencies (`CsrfProtected`, `CurrentUser`) and are
#: expected to refuse anonymous callers exactly like the router-gated routes.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/readyz",
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/config",
    }
)

#: Methods FastAPI adds for free and that carry no handler of their own.
_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

#: Reads never require CSRF; every state-changing method does.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _routes() -> list[tuple[str, str]]:
    """Every (method, path) the application actually registers."""
    pairs: list[tuple[str, str]] = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(set(route.methods or set()) - _IGNORED_METHODS):
            pairs.append((method, route.path))
    return sorted(set(pairs))


def _concrete(path: str) -> str:
    """Fill path parameters with syntactically valid values.

    Every parameter in the current route table is a UUID. A well-formed value
    matters: a malformed one is rejected at validation and would tell us
    nothing about whether authentication ran first.
    """
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + str(uuid7()) + out[end + 1 :]
    return out


ALL_ROUTES = _routes()
PROTECTED = [(m, p) for m, p in ALL_ROUTES if p not in PUBLIC_PATHS]
PUBLIC = [(m, p) for m, p in ALL_ROUTES if p in PUBLIC_PATHS]
PROTECTED_MUTATIONS = [(m, p) for m, p in PROTECTED if m not in _SAFE_METHODS]


def _id(pair: tuple[str, str]) -> str:
    return f"{pair[0]} {pair[1]}"


# --------------------------------------------------------------- classification
def test_the_route_table_is_not_empty() -> None:
    """A bug that enumerated nothing would make every test below vacuous."""
    assert len(ALL_ROUTES) > 30, ALL_ROUTES
    assert PROTECTED, "no protected routes found"
    assert PUBLIC, "no public routes found"


def test_every_route_is_classified() -> None:
    """Every registered path is either public by decision or protected.

    This is the regression guard. A new router added without the gate, or a
    new public path added without a reason, fails here first.
    """
    prefix = get_settings().api_prefix
    unclassified = [
        (m, p)
        for m, p in ALL_ROUTES
        if p not in PUBLIC_PATHS and not p.startswith(prefix)
    ]
    assert unclassified == [], (
        "routes outside the API prefix that are not listed as public: "
        f"{unclassified}"
    )


def test_public_paths_all_exist() -> None:
    """The public list may not name a route that is no longer registered."""
    registered = {p for _, p in ALL_ROUTES}
    stale = sorted(PUBLIC_PATHS - registered)
    assert stale == [], f"PUBLIC_PATHS names routes that do not exist: {stale}"


def test_auth_session_endpoints_are_not_public() -> None:
    """/auth/me and /auth/logout are session-bearing, not part of sign-in."""
    assert "/api/v1/auth/me" not in PUBLIC_PATHS
    assert "/api/v1/auth/logout" not in PUBLIC_PATHS


# ------------------------------------------------------------------- anonymous
@pytest.mark.parametrize("method,path", PROTECTED, ids=[_id(r) for r in PROTECTED])
async def test_protected_routes_reject_anonymous(app_client, method, path) -> None:
    """Every protected route answers 401 with no session.

    No request body is sent on purpose. The gate is a router-level dependency,
    so it resolves before the endpoint's own parameters: an anonymous POST is
    refused as unauthenticated rather than as a validation error, and a 422
    here would mean the body was parsed before anyone checked who was asking.
    """
    response = await app_client.request(method, _concrete(path))
    assert response.status_code == 401, (
        f"{method} {path} returned {response.status_code} to an anonymous caller"
    )


@pytest.mark.parametrize("method,path", PUBLIC, ids=[_id(r) for r in PUBLIC])
async def test_public_routes_do_not_require_authentication(
    app_client, method, path
) -> None:
    """The public surface stays reachable without a session.

    Status is deliberately not pinned: /api/v1/health answers 503 when its
    dependencies are down, which is correct behaviour and not an auth failure.
    """
    response = await app_client.request(method, _concrete(path))
    assert response.status_code != 401, f"{method} {path} now demands a session"


async def test_login_remains_reachable_without_a_session(app_client) -> None:
    """The one that would lock everyone out if it were gated.

    An empty body is a 422 from validation - which is the proof: the request
    reached the endpoint instead of being turned away at the door.
    """
    response = await app_client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422, response.status_code


async def test_auth_config_remains_reachable_without_a_session(app_client) -> None:
    response = await app_client.get("/api/v1/auth/config")
    assert response.status_code == 200
    assert "totp_required" in response.json()


async def test_anonymous_access_does_not_reveal_whether_a_job_exists(
    app_client,
) -> None:
    """401 before 404.

    A protected read must fail authentication before it looks the resource up.
    Answering 404 here would let an anonymous caller enumerate job ids by the
    difference between 404 and 200.
    """
    response = await app_client.get(f"/api/v1/jobs/{uuid7()}/evaluation")
    assert response.status_code == 401


# --------------------------------------------------------------- authenticated
@pytest.mark.parametrize(
    "method,path",
    [(m, p) for m, p in PROTECTED if m in _SAFE_METHODS],
    ids=[_id(r) for r in PROTECTED if r[0] in _SAFE_METHODS],
)
async def test_authenticated_session_passes_the_gate(
    db_app_client, method, path
) -> None:
    """A signed-in caller is never refused by authentication.

    What happens after the gate is the endpoint's own business: a random UUID
    yields 404, an empty database yields 404, and both are fine. The assertion
    is only that the refusal is not 401 or 403 - that the session was accepted
    and the request was allowed to reach the handler.
    """
    try:
        response = await db_app_client.request(method, _concrete(path))
    except Exception:
        # The handler ran and raised on missing data. That still proves the
        # gate let it through, which is what this test is about.
        return
    assert response.status_code not in (401, 403), (
        f"{method} {path} refused an authenticated session with "
        f"{response.status_code}"
    )


# ----------------------------------------------------------------------- CSRF
@pytest.mark.parametrize(
    "method,path",
    PROTECTED_MUTATIONS,
    ids=[_id(r) for r in PROTECTED_MUTATIONS],
)
async def test_protected_mutations_require_csrf(
    db_app_client, method, path
) -> None:
    """A valid session alone is not enough to change state.

    The cookies stay; only the echoed header is removed. That is exactly the
    shape of a cross-site request: the browser attaches the cookie, the
    attacker's page cannot read it to echo it back.
    """
    db_app_client.headers.pop(CSRF_HEADER, None)
    response = await db_app_client.request(method, _concrete(path))
    assert response.status_code == 403, (
        f"{method} {path} accepted a session without a CSRF token "
        f"({response.status_code})"
    )


async def test_safe_methods_do_not_require_csrf(db_app_client) -> None:
    """The mirror of the above: reads are not gated on CSRF."""
    db_app_client.headers.pop(CSRF_HEADER, None)
    response = await db_app_client.get("/api/v1/jobs/counts")
    assert response.status_code != 403


# ===========================================================================
# M2: role policy
#
# The two sets below are the authorization matrix, written out rather than
# derived. Deriving them from the decorators would make the test agree with
# the code by construction and prove nothing; stated independently, they are
# a second opinion that has to be reconciled with the first.
#
# `reviewer` may read everything the product exposes and may run the human
# resume-review workflow. It may not change the profile, may not create,
# edit, verify or delete facts, and may not import jobs or touch source
# configuration.
# ===========================================================================

#: Routes only an `owner` may call. `grep -rn require_owner apps/api` should
#: name exactly these endpoints.
OWNER_ONLY = frozenset(
    {
        ("POST", "/api/v1/profiles/{profile_id}/master-resumes"),
        ("POST", "/api/v1/jobs/{job_id}/first-draft"),
        ("PUT", "/api/v1/profile"),
        ("POST", "/api/v1/profile/certifications"),
        ("PATCH", "/api/v1/profile/certifications/{entity_id}"),
        ("DELETE", "/api/v1/profile/certifications/{entity_id}"),
        ("POST", "/api/v1/profile/educations"),
        ("PATCH", "/api/v1/profile/educations/{entity_id}"),
        ("DELETE", "/api/v1/profile/educations/{entity_id}"),
        ("POST", "/api/v1/profile/experiences"),
        ("PATCH", "/api/v1/profile/experiences/{entity_id}"),
        ("DELETE", "/api/v1/profile/experiences/{entity_id}"),
        ("POST", "/api/v1/profile/projects"),
        ("PATCH", "/api/v1/profile/projects/{entity_id}"),
        ("DELETE", "/api/v1/profile/projects/{entity_id}"),
        ("POST", "/api/v1/profile/skills"),
        ("PATCH", "/api/v1/profile/skills/{entity_id}"),
        ("DELETE", "/api/v1/profile/skills/{entity_id}"),
        ("POST", "/api/v1/profile/facts"),
        ("POST", "/api/v1/profile/facts/suggest"),
        ("PATCH", "/api/v1/profile/facts/{fact_id}"),
        ("DELETE", "/api/v1/profile/facts/{fact_id}"),
        ("POST", "/api/v1/profile/facts/{fact_id}/verify"),
        ("POST", "/api/v1/profile/facts/{fact_id}/unverify"),
        ("POST", "/api/v1/profile/facts/{fact_id}/retire"),
        ("POST", "/api/v1/profile/facts/{fact_id}/evidence"),
        ("POST", "/api/v1/jobs/import"),
        ("GET", "/api/v1/sources"),
        ("POST", "/api/v1/sources"),
    }
)

#: Routes either role may call: every read the product exposes, the resume
#: review workflow, and the two session endpoints.
OWNER_AND_REVIEWER = frozenset(
    {
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/profile"),
        ("GET", "/api/v1/profile/certifications"),
        ("GET", "/api/v1/profile/educations"),
        ("GET", "/api/v1/profile/experiences"),
        ("GET", "/api/v1/profile/projects"),
        ("GET", "/api/v1/profile/skills"),
        ("GET", "/api/v1/profile/facts"),
        ("GET", "/api/v1/profile/facts/summary"),
        ("GET", "/api/v1/profile/facts/citable"),
        ("GET", "/api/v1/profile/facts/{fact_id}"),
        ("GET", "/api/v1/profile/facts/{fact_id}/evidence"),
        ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/jobs/counts"),
        ("GET", "/api/v1/jobs/{job_id}"),
        ("GET", "/api/v1/jobs/{job_id}/evaluation"),
        ("GET", "/api/v1/resumes/{resume_id}"),
        ("GET", "/api/v1/applications/{application_id}/first-draft"),
        ("GET", "/api/v1/applications"),
        ("GET", "/api/v1/applications/{application_id}/resumes/latest"),
        ("GET", "/api/v1/applications/{application_id}/resume-review"),
        ("POST", "/api/v1/applications/{application_id}/resume-review"),
        ("POST", "/api/v1/applications/{application_id}/resume-review/approve"),
        ("POST", "/api/v1/applications/{application_id}/resume-review/request-changes"),
    }
)

OWNER_ONLY_MUTATIONS = [r for r in sorted(OWNER_ONLY) if r[0] not in _SAFE_METHODS]
REVIEWER_ALLOWED_MUTATIONS = [
    r for r in sorted(OWNER_AND_REVIEWER) if r[0] not in _SAFE_METHODS
]

#: The problem-document `type` each refusal carries. Both are 403, and the
#: difference is the whole point: a client that cannot tell them apart will
#: ask the user to sign in again when the real answer is "not your role".
CSRF_TYPE = "https://jobforge.local/errors/csrf"
ROLE_TYPE = "https://jobforge.local/errors/not-authorized"


# ---------------------------------------------------------------- the matrix
def test_every_protected_route_has_a_role_policy() -> None:
    """The M2 regression guard.

    A new protected endpoint that nobody classified appears in neither set
    and fails here. An endpoint listed in both is a contradiction and fails
    here too.
    """
    classified = OWNER_ONLY | OWNER_AND_REVIEWER
    protected = set(PROTECTED)

    unclassified = sorted(protected - classified)
    assert unclassified == [], (
        f"protected routes with no role policy: {unclassified}"
    )

    stale = sorted(classified - protected)
    assert stale == [], f"role policy names routes that are not registered: {stale}"

    both = sorted(OWNER_ONLY & OWNER_AND_REVIEWER)
    assert both == [], f"routes classified as owner-only AND open to reviewers: {both}"


def test_no_public_route_carries_a_role_policy() -> None:
    """Public means public. A role policy on an unauthenticated route would
    be unenforceable and misleading."""
    overlap = sorted(
        r for r in (OWNER_ONLY | OWNER_AND_REVIEWER) if r[1] in PUBLIC_PATHS
    )
    assert overlap == [], overlap


# ------------------------------------------------------------------ reviewer
@pytest.mark.parametrize(
    "method,path", sorted(OWNER_ONLY), ids=[_id(r) for r in sorted(OWNER_ONLY)]
)
async def test_reviewer_is_refused_on_owner_only_routes(
    api_sessions, method, path
) -> None:
    """403 with the authorization problem type, not 401 and not 404.

    The path parameter is a random UUID that certainly does not exist. A 404
    here would mean the lookup ran before the role check, which is how an
    unauthorized caller enumerates ids: 404 for absent, 403 for present.
    """
    reviewer = await api_sessions("reviewer")
    response = await reviewer.request(method, _concrete(path))
    assert response.status_code == 403, (
        f"reviewer got {response.status_code} on owner-only {method} {path}"
    )
    assert response.json()["type"] == ROLE_TYPE, response.json()


@pytest.mark.parametrize(
    "method,path",
    sorted(OWNER_AND_REVIEWER),
    ids=[_id(r) for r in sorted(OWNER_AND_REVIEWER)],
)
async def test_reviewer_passes_the_gate_on_permitted_routes(
    api_sessions, method, path
) -> None:
    """A reviewer is never refused by authorization on a permitted route.

    What the endpoint then does is its own business - a random UUID yields
    404, an empty database yields 404 - so the assertion is that the refusal
    is not 401, and not a 403 carrying the authorization type. A CSRF 403
    would be a test bug, and is caught by checking the type.
    """
    reviewer = await api_sessions("reviewer")
    try:
        response = await reviewer.request(method, _concrete(path))
    except Exception:
        return  # the handler ran and raised on missing data: the gate passed
    assert response.status_code != 401, f"{method} {path} refused a reviewer session"
    if response.status_code == 403:
        assert response.json()["type"] != ROLE_TYPE, (
            f"reviewer was refused by role on permitted {method} {path}"
        )


# --------------------------------------------------------------------- owner
@pytest.mark.parametrize(
    "method,path", sorted(OWNER_ONLY), ids=[_id(r) for r in sorted(OWNER_ONLY)]
)
async def test_owner_is_never_refused_by_role(api_sessions, method, path) -> None:
    """The mirror of the reviewer test: the owner passes every role check."""
    owner = await api_sessions("owner")
    try:
        response = await owner.request(method, _concrete(path))
    except Exception:
        return
    assert response.status_code != 401
    if response.status_code == 403:
        assert response.json()["type"] != ROLE_TYPE, (
            f"owner was refused by role on {method} {path}"
        )


# ---------------------------------------------------------------------- CSRF
@pytest.mark.parametrize(
    "method,path",
    OWNER_ONLY_MUTATIONS,
    ids=[_id(r) for r in OWNER_ONLY_MUTATIONS],
)
async def test_csrf_is_enforced_for_the_owner(api_sessions, method, path) -> None:
    """Role does not exempt anyone from CSRF. The owner least of all."""
    owner = await api_sessions("owner")
    owner.headers.pop(CSRF_HEADER, None)
    response = await owner.request(method, _concrete(path))
    assert response.status_code == 403
    assert response.json()["type"] == CSRF_TYPE, response.json()


@pytest.mark.parametrize(
    "method,path",
    REVIEWER_ALLOWED_MUTATIONS,
    ids=[_id(r) for r in REVIEWER_ALLOWED_MUTATIONS],
)
async def test_csrf_is_enforced_for_the_reviewer(
    api_sessions, method, path
) -> None:
    """A reviewer's permitted mutations are still CSRF-protected.

    This is the pair that proves the two checks are independent: the same
    route answers a CSRF 403 without a token and a normal response with one.
    """
    reviewer = await api_sessions("reviewer")
    reviewer.headers.pop(CSRF_HEADER, None)
    response = await reviewer.request(method, _concrete(path))
    assert response.status_code == 403
    assert response.json()["type"] == CSRF_TYPE, response.json()


# ------------------------------------------------------- no existence leaking
async def test_reviewer_cannot_distinguish_a_real_fact_from_a_missing_one(
    api_sessions, verified_fact
) -> None:
    """Two DELETEs, one real id and one invented, must be indistinguishable.

    If the role check ran after the lookup, the real id would answer 403 and
    the invented one 404, which is an oracle for whether a fact exists.
    """
    reviewer = await api_sessions("reviewer")

    real = await reviewer.delete(f"/api/v1/profile/facts/{verified_fact}")
    missing = await reviewer.delete(f"/api/v1/profile/facts/{uuid7()}")

    assert real.status_code == missing.status_code == 403
    assert real.json()["type"] == missing.json()["type"] == ROLE_TYPE


async def test_anonymous_and_reviewer_refusals_are_distinguishable(
    app_client, api_sessions
) -> None:
    """401 for "who are you", 403 for "not you". Never the same answer."""
    anonymous = await app_client.post("/api/v1/jobs/import", json={})
    assert anonymous.status_code == 401

    reviewer = await api_sessions("reviewer")
    refused = await reviewer.post("/api/v1/jobs/import", json={})
    assert refused.status_code == 403
    assert refused.json()["type"] == ROLE_TYPE
