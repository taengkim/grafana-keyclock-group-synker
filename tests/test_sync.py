"""Unit tests for sync.py with all external HTTP calls mocked via `responses`.

Group model under test: /service/<svc>/<svc>_<role>
  - "service" (matches GROUP_PREFIX) is the root group
  - depth 1 below the root = service (direct members -> team "<svc>")
  - depth 2 = role groups ("abc_adm", "abc_editor", "abc_viewer"), each
    synced as an independent Grafana team named after the role group;
    folder permissions are granted to those teams outside this tool
"""
import json
import logging
import sys
from pathlib import Path

import pytest
import responses
from responses import matchers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

KC = "https://keycloak.example.com"
GF = "https://grafana.example.com"
REALM = "master"
ADMIN = f"{KC}/admin/realms/{REALM}"
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"

ROOT = {"id": "root", "name": "service"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def capture_info_logs(caplog):
    caplog.set_level(logging.INFO)


def make_config(**overrides):
    defaults = dict(
        keycloak_url=KC,
        keycloak_realm=REALM,
        keycloak_client_id="grafana-sync",
        keycloak_client_secret="secret",
        grafana_url=GF,
        grafana_token="gf-token",
        group_prefix="service",
        match_key="email",
        dry_run=False,
        max_removal_ratio=0.5,
    )
    defaults.update(overrides)
    return sync.Config(**defaults)


def kc_user(email, username=None, enabled=True):
    username = username or email.split("@")[0]
    return {"id": f"kc-{username}", "username": username, "email": email, "enabled": enabled}


def gf_member(user_id, email, login=None):
    return {"userId": user_id, "email": email, "login": login or email.split("@")[0]}


def add_token_endpoint():
    responses.add(responses.POST, TOKEN_URL, json={"access_token": "kc-token", "expires_in": 60})


def add_tree(service_groups, empty_children=True):
    """Register the root group listing and its service children.

    With empty_children, each service group gets an empty role-group
    list; tests that need role groups register their own.
    """
    responses.add(responses.GET, f"{ADMIN}/groups", json=[ROOT])
    responses.add(responses.GET, f"{ADMIN}/groups/{ROOT['id']}/children", json=service_groups)
    if empty_children:
        for group in service_groups:
            responses.add(responses.GET, f"{ADMIN}/groups/{group['id']}/children", json=[])


def add_children(group_id, children):
    responses.add(responses.GET, f"{ADMIN}/groups/{group_id}/children", json=children)


def add_members(group_id, users):
    responses.add(responses.GET, f"{ADMIN}/groups/{group_id}/members", json=users)


def add_team_search(name, team=None):
    teams = [team] if team else []
    responses.add(
        responses.GET, f"{GF}/api/teams/search",
        match=[matchers.query_param_matcher({"name": name})],
        json={"totalCount": len(teams), "teams": teams},
    )


def add_lookup(key, user_id=None):
    if user_id is None:
        responses.add(
            responses.GET, f"{GF}/api/users/lookup",
            match=[matchers.query_param_matcher({"loginOrEmail": key})],
            json={"message": "user not found"}, status=404,
        )
    else:
        responses.add(
            responses.GET, f"{GF}/api/users/lookup",
            match=[matchers.query_param_matcher({"loginOrEmail": key})],
            json={"id": user_id, "email": key},
        )


def write_calls():
    return [c for c in responses.calls if c.request.method in ("POST", "DELETE", "PUT", "PATCH")]


def grafana_write_calls():
    return [c for c in write_calls() if c.request.url.startswith(GF)]


def created_team_names():
    return [
        json.loads(c.request.body)["name"]
        for c in responses.calls
        if c.request.method == "POST" and c.request.url == f"{GF}/api/teams"
    ]


@responses.activate
def test_creates_new_team_and_adds_members():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com"), kc_user("bob@example.com")])
    add_team_search("devs")  # team does not exist yet
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_lookup("alice@example.com", user_id=101)
    add_lookup("bob@example.com", user_id=102)
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config()) == 0
    assert added.call_count == 2


@responses.activate
def test_role_groups_become_independent_teams():
    """/service/abc/{abc_adm,abc_editor,abc_viewer} -> three role teams."""
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "abc"}], empty_children=False)
    add_children("g1", [
        {"id": "g2", "name": "abc_adm"},
        {"id": "g3", "name": "abc_editor"},
        {"id": "g4", "name": "abc_viewer"},
    ])
    add_members("g1", [])  # nobody directly in the service group
    add_members("g2", [kc_user("alice@example.com")])
    add_members("g3", [kc_user("bob@example.com")])
    add_members("g4", [kc_user("carol@example.com")])
    add_team_search("abc")
    add_team_search("abc_adm")
    add_team_search("abc_editor")
    add_team_search("abc_viewer")
    responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_lookup("alice@example.com", user_id=101)
    add_lookup("bob@example.com", user_id=102)
    add_lookup("carol@example.com", user_id=103)
    responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config()) == 0
    # The empty "abc" service team is not created; the three role teams are
    assert sorted(created_team_names()) == ["abc_adm", "abc_editor", "abc_viewer"]


@responses.activate
def test_service_direct_members_sync_to_service_team():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "abc"}], empty_children=False)
    add_children("g1", [{"id": "g2", "name": "abc_viewer"}])
    add_members("g1", [kc_user("dave@example.com")])
    add_members("g2", [kc_user("erin@example.com")])
    add_team_search("abc", {"id": 7, "name": "abc"})
    add_team_search("abc_viewer", {"id": 8, "name": "abc_viewer"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(104, "dave@example.com")])
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[gf_member(105, "erin@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()


@responses.activate
def test_empty_service_team_is_not_created(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "abc"}], empty_children=False)
    add_children("g1", [{"id": "g2", "name": "abc_adm"}])
    add_members("g1", [])
    add_members("g2", [kc_user("alice@example.com")])
    add_team_search("abc")      # does not exist and has no desired members
    add_team_search("abc_adm", {"id": 8, "name": "abc_adm"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()
    assert "empty_team_not_created" in caplog.text


@responses.activate
def test_unknown_role_group_is_skipped(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}], empty_children=False)
    add_children("g1", [{"id": "g2", "name": "devs_leads"}, {"id": "g3", "name": "other_adm"}])
    add_members("g1", [kc_user("alice@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert "unknown_role_group_skipped" in caplog.text
    # Members of unknown role groups were never even fetched
    assert not [c for c in responses.calls if "/groups/g2/members" in c.request.url]
    assert not [c for c in responses.calls if "/groups/g3/members" in c.request.url]
    assert not grafana_write_calls()


@responses.activate
def test_custom_role_suffixes():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "abc"}], empty_children=False)
    add_children("g1", [{"id": "g2", "name": "abc_ops"}])
    add_members("g1", [])
    add_members("g2", [kc_user("alice@example.com")])
    add_team_search("abc")
    add_team_search("abc_ops")
    created = responses.add(responses.POST, f"{GF}/api/teams", json={"teamId": 7, "message": "Team created"})
    add_lookup("alice@example.com", user_id=101)
    responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})

    assert sync.run_sync(make_config(role_suffixes=frozenset({"ops"}))) == 0
    assert created.call_count == 1
    assert created_team_names() == ["abc_ops"]


@responses.activate
def test_removes_member_no_longer_in_group():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(101, "alice@example.com"), gf_member(102, "bob@example.com")],
    )
    removed = responses.add(responses.DELETE, f"{GF}/api/teams/7/members/102", json={"message": "Member removed"})

    # 1 removal out of 2 members = 0.5, not above the 0.5 guard threshold
    assert sync.run_sync(make_config()) == 0
    assert removed.call_count == 1
    assert len(grafana_write_calls()) == 1


@responses.activate
def test_add_and_remove_in_same_team():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com"), kc_user("carol@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(101, "alice@example.com"), gf_member(102, "bob@example.com")],
    )
    add_lookup("carol@example.com", user_id=103)
    added = responses.add(responses.POST, f"{GF}/api/teams/7/members", json={"message": "Member added"})
    removed = responses.add(responses.DELETE, f"{GF}/api/teams/7/members/102", json={"message": "Member removed"})

    assert sync.run_sync(make_config()) == 0
    assert added.call_count == 1
    assert removed.call_count == 1


@responses.activate
def test_user_not_yet_in_grafana_is_skipped_as_pending(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com"), kc_user("newbie@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])
    add_lookup("newbie@example.com", user_id=None)  # 404: never logged in

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()
    assert "member_pending_first_login" in caplog.text
    assert "pending_first_login=1" in caplog.text


@responses.activate
def test_no_root_group_changes_nothing(caplog):
    add_token_endpoint()
    responses.add(responses.GET, f"{ADMIN}/groups", json=[{"id": "g1", "name": "engineering"}, {"id": "g2", "name": "sales"}])
    add_children("g1", [])
    add_children("g2", [])

    assert sync.run_sync(make_config()) == 0
    assert all(not c.request.url.startswith(GF) for c in responses.calls)
    assert "no_managed_groups" in caplog.text


@responses.activate
def test_root_without_services_changes_nothing(caplog):
    add_token_endpoint()
    add_tree([])

    assert sync.run_sync(make_config()) == 0
    assert all(not c.request.url.startswith(GF) for c in responses.calls)
    assert "no_managed_groups" in caplog.text


@responses.activate
def test_removal_ratio_guard_skips_team_and_exits_1(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}, {"id": "g2", "name": "ops"}])
    # devs: 3 of 4 members would be removed -> 0.75 > 0.5 -> guard
    add_members("g1", [kc_user("alice@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[
            gf_member(101, "alice@example.com"),
            gf_member(102, "bob@example.com"),
            gf_member(103, "carol@example.com"),
            gf_member(104, "dave@example.com"),
        ],
    )
    # ops: unaffected, still processed normally
    add_members("g2", [kc_user("erin@example.com")])
    add_team_search("ops", {"id": 8, "name": "ops"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[])
    add_lookup("erin@example.com", user_id=105)
    ops_add = responses.add(responses.POST, f"{GF}/api/teams/8/members", json={"message": "Member added"})

    assert sync.run_sync(make_config()) == 1
    assert "removal_guard_triggered" in caplog.text
    assert not [c for c in responses.calls if c.request.method == "DELETE"]
    assert ops_add.call_count == 1


@responses.activate
def test_subgroups_fallback_for_old_keycloak():
    add_token_endpoint()
    responses.add(
        responses.GET, f"{ADMIN}/groups",
        json=[{
            "id": "root", "name": "service",
            "subGroups": [{
                "id": "g1", "name": "devs",
                "subGroups": [{"id": "g2", "name": "devs_adm", "subGroups": []}],
            }],
        }],
    )
    # Old Keycloak: /children does not exist; subGroups fallback kicks in
    responses.add(responses.GET, f"{ADMIN}/groups/root/children", status=404, json={"error": "unknown_error"})
    add_members("g1", [kc_user("alice@example.com")])
    add_members("g2", [kc_user("bob@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    add_team_search("devs_adm", {"id": 8, "name": "devs_adm"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[gf_member(102, "bob@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()


@responses.activate
def test_member_pagination_over_multiple_pages():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "big"}])
    page1 = [kc_user(f"user{i}@example.com") for i in range(100)]
    page2 = [kc_user(f"user{i}@example.com") for i in range(100, 105)]
    responses.add(
        responses.GET, f"{ADMIN}/groups/g1/members",
        match=[matchers.query_param_matcher({"first": "0", "max": "100"})], json=page1,
    )
    responses.add(
        responses.GET, f"{ADMIN}/groups/g1/members",
        match=[matchers.query_param_matcher({"first": "100", "max": "100"})], json=page2,
    )
    add_team_search("big", {"id": 9, "name": "big"})
    # All 105 already members: pagination worked iff nobody is added/removed
    responses.add(
        responses.GET, f"{GF}/api/teams/9/members",
        json=[gf_member(1000 + i, f"user{i}@example.com") for i in range(105)],
    )

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()
    member_calls = [c for c in responses.calls if "/groups/g1/members" in c.request.url]
    assert len(member_calls) == 2


@responses.activate
def test_dry_run_makes_no_write_calls(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}, {"id": "g2", "name": "new"}], empty_children=False)
    add_children("g1", [])
    add_children("g2", [])
    add_members("g1", [kc_user("carol@example.com")])
    add_members("g2", [kc_user("alice@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(
        responses.GET, f"{GF}/api/teams/7/members",
        json=[gf_member(102, "bob@example.com"), gf_member(101, "old@example.com")],
    )
    add_team_search("new")  # would need to be created
    add_lookup("carol@example.com", user_id=103)
    add_lookup("alice@example.com", user_id=101)

    assert sync.run_sync(make_config(dry_run=True, max_removal_ratio=1.0)) == 0
    assert not grafana_write_calls()
    assert "would_create_team" in caplog.text
    assert "would_add_member" in caplog.text
    assert "would_remove_member" in caplog.text


@responses.activate
def test_disabled_users_are_excluded():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com"), kc_user("gone@example.com", enabled=False)])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert not grafana_write_calls()


@responses.activate
def test_keycloak_token_refresh_on_401():
    token = responses.add(responses.POST, TOKEN_URL, json={"access_token": "kc-token", "expires_in": 60})
    # First admin call rejects the token, retry after refresh succeeds
    responses.add(responses.GET, f"{ADMIN}/groups", status=401, json={"error": "invalid_token"})
    responses.add(responses.GET, f"{ADMIN}/groups", json=[ROOT])
    responses.add(responses.GET, f"{ADMIN}/groups/{ROOT['id']}/children", json=[{"id": "g1", "name": "devs"}])
    add_children("g1", [])
    add_members("g1", [kc_user("alice@example.com")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[gf_member(101, "alice@example.com")])

    assert sync.run_sync(make_config()) == 0
    assert token.call_count == 2


@responses.activate
def test_missing_match_key_user_is_skipped(caplog):
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [{"id": "kc-x", "username": "no-email-user", "email": None, "enabled": True}])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[])

    assert sync.run_sync(make_config()) == 0
    assert "member_missing_match_key" in caplog.text
    assert not grafana_write_calls()


@responses.activate
def test_match_key_username_uses_login_and_is_case_insensitive():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com", username="Alice")])
    add_team_search("devs", {"id": 7, "name": "devs"})
    responses.add(responses.GET, f"{GF}/api/teams/7/members", json=[{"userId": 101, "email": "other@example.com", "login": "alice"}])

    assert sync.run_sync(make_config(match_key="username")) == 0
    assert not grafana_write_calls()


@responses.activate
def test_retry_on_5xx_then_success():
    add_token_endpoint()
    responses.add(responses.GET, f"{ADMIN}/groups", status=502, json={"error": "bad gateway"})
    responses.add(responses.GET, f"{ADMIN}/groups", json=[])

    assert sync.run_sync(make_config()) == 0


@responses.activate
def test_grafana_auth_failure_exits_2():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}])
    add_members("g1", [kc_user("alice@example.com")])
    responses.add(responses.GET, f"{GF}/api/teams/search", status=401, json={"message": "Unauthorized"})

    env = {
        "KEYCLOAK_URL": KC,
        "KEYCLOAK_REALM": REALM,
        "KEYCLOAK_CLIENT_ID": "grafana-sync",
        "KEYCLOAK_CLIENT_SECRET": "secret",
        "GRAFANA_URL": GF,
        "GRAFANA_TOKEN": "gf-token",
        "GROUP_PREFIX": "service",
        "DRY_RUN": "false",
    }
    assert sync.main(env) == 2


def test_config_missing_required_vars_exits_2():
    assert sync.main({"KEYCLOAK_URL": KC}) == 2


def test_config_defaults_dry_run_true():
    cfg = sync.Config.from_env({
        "KEYCLOAK_URL": KC,
        "KEYCLOAK_REALM": REALM,
        "KEYCLOAK_CLIENT_ID": "grafana-sync",
        "KEYCLOAK_CLIENT_SECRET": "secret",
        "GRAFANA_URL": GF,
        "GRAFANA_TOKEN": "gf-token",
    })
    assert cfg.dry_run is True
    assert cfg.group_prefix == "grafana-"
    assert cfg.match_key == "email"
    assert cfg.max_removal_ratio == 0.5
    assert cfg.role_suffixes == frozenset(sync.DEFAULT_ROLE_SUFFIXES)


def test_config_parses_role_suffixes():
    cfg = sync.Config.from_env({
        "KEYCLOAK_URL": KC,
        "KEYCLOAK_REALM": REALM,
        "KEYCLOAK_CLIENT_ID": "grafana-sync",
        "KEYCLOAK_CLIENT_SECRET": "secret",
        "GRAFANA_URL": GF,
        "GRAFANA_TOKEN": "gf-token",
        "ROLE_SUFFIXES": "adm, Editor ,viewer",
    })
    assert cfg.role_suffixes == frozenset({"adm", "editor", "viewer"})


def test_config_invalid_role_suffixes():
    with pytest.raises(sync.ConfigError):
        sync.parse_role_suffixes("  ,  ")


def test_config_invalid_match_key():
    with pytest.raises(sync.ConfigError):
        sync.Config.from_env({
            "KEYCLOAK_URL": KC,
            "KEYCLOAK_REALM": REALM,
            "KEYCLOAK_CLIENT_ID": "grafana-sync",
            "KEYCLOAK_CLIENT_SECRET": "secret",
            "GRAFANA_URL": GF,
            "GRAFANA_TOKEN": "gf-token",
            "MATCH_KEY": "displayName",
        })


SUFFIXES = frozenset(sync.DEFAULT_ROLE_SUFFIXES)


@pytest.mark.parametrize("service,child,expected", [
    ("abc", "abc_adm", True),
    ("abc", "abc_admin", True),
    ("abc", "abc_editor", True),
    ("abc", "abc_viewer", True),
    ("abc", "abc-viewer", True),
    ("abc", "ABC_ADM", True),
    ("abc", "abc_leads", False),
    ("abc", "xyz_adm", False),   # different service name
    ("abc", "adm", False),       # bare suffix without service prefix
    ("abc", "abc", False),
])
def test_is_role_group(service, child, expected):
    assert sync.is_role_group(service, child, SUFFIXES) is expected


@responses.activate
def test_one_failed_team_does_not_stop_others():
    add_token_endpoint()
    add_tree([{"id": "g1", "name": "devs"}, {"id": "g2", "name": "ops"}])
    add_members("g1", [kc_user("alice@example.com")])
    add_members("g2", [kc_user("erin@example.com")])
    # devs search keeps failing with a non-retryable client error
    responses.add(
        responses.GET, f"{GF}/api/teams/search",
        match=[matchers.query_param_matcher({"name": "devs"})],
        status=422, json={"message": "boom"},
    )
    add_team_search("ops", {"id": 8, "name": "ops"})
    responses.add(responses.GET, f"{GF}/api/teams/8/members", json=[])
    add_lookup("erin@example.com", user_id=105)
    ops_add = responses.add(responses.POST, f"{GF}/api/teams/8/members", json={"message": "Member added"})

    assert sync.run_sync(make_config()) == 1
    assert ops_add.call_count == 1
