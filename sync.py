#!/usr/bin/env python3
"""Sync Keycloak group memberships to Grafana teams.

Groups whose name starts with GROUP_PREFIX are root groups. One depth
below a root group are the services; one depth below a service are its
role groups, each synced as an INDEPENDENT Grafana team, e.g. with
GROUP_PREFIX=service the tree /service/abc/{abc_adm,abc_editor,
abc_viewer} yields Grafana teams "abc_adm", "abc_editor" and
"abc_viewer". Folder permissions (Admin/Edit/View) are granted to
those teams outside this tool (Terraform or manually). Direct members
of the service group itself sync to a team named after the service.
Only team membership is managed here; org roles stay with Grafana's
role_attribute_path mapping.

Exit codes:
    0 - success
    1 - partial failure (removal guard triggered or some teams failed)
    2 - configuration error or authentication failure
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger("grafana-team-sync")

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
PAGE_SIZE = 100

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2

# Default role-group name suffixes recognized under a service group
# (e.g. "abc_adm" for service "abc"). Overridable via ROLE_SUFFIXES.
DEFAULT_ROLE_SUFFIXES = ("adm", "admin", "editor", "viewer", "member", "mbr")


def parse_role_suffixes(raw: str) -> frozenset[str]:
    """Parse ROLE_SUFFIXES: comma-separated suffixes, e.g. "adm,editor,viewer"."""
    suffixes = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not suffixes:
        raise ConfigError("ROLE_SUFFIXES must contain at least one suffix")
    return frozenset(suffixes)


def is_role_group(service_name: str, child_name: str, role_suffixes: frozenset[str]) -> bool:
    """Whether a service sub-group is a recognized role group.

    Matches "<service>_<suffix>" or "<service>-<suffix>" with a suffix
    from role_suffixes, case-insensitive (e.g. "abc_adm", "abc-viewer").
    """
    name = child_name.strip().lower()
    service = service_name.strip().lower()
    for separator in ("_", "-"):
        prefixed = f"{service}{separator}"
        if name.startswith(prefixed):
            return name[len(prefixed):] in role_suffixes
    return False

RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


class ConfigError(Exception):
    """Invalid or missing configuration."""


class AuthError(Exception):
    """Authentication or authorization failure against Keycloak or Grafana."""


class ApiError(Exception):
    """Unexpected API response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _fmt(value: object) -> str:
    text = str(value)
    if text == "" or any(c in text for c in (" ", '"', "=")):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def log_event(level: int, event: str, **fields: object) -> None:
    """Emit one structured logfmt-style line: event=... key=value ..."""
    parts = [f"event={_fmt(event)}"]
    parts.extend(f"{key}={_fmt(value)}" for key, value in fields.items())
    logger.log(level, " ".join(parts))


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("time=%(asctime)s level=%(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def parse_bool(raw: str, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


@dataclass
class Config:
    keycloak_url: str
    keycloak_realm: str
    keycloak_client_id: str
    keycloak_client_secret: str
    grafana_url: str
    grafana_token: str
    group_prefix: str = "grafana-"
    match_key: str = "email"
    dry_run: bool = True
    max_removal_ratio: float = 0.5
    log_level: str = "INFO"
    role_suffixes: frozenset = frozenset(DEFAULT_ROLE_SUFFIXES)

    REQUIRED = (
        "KEYCLOAK_URL",
        "KEYCLOAK_REALM",
        "KEYCLOAK_CLIENT_ID",
        "KEYCLOAK_CLIENT_SECRET",
        "GRAFANA_URL",
        "GRAFANA_TOKEN",
    )

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        missing = [name for name in cls.REQUIRED if not env.get(name)]
        if missing:
            raise ConfigError("missing required environment variables: " + ", ".join(missing))

        match_key = env.get("MATCH_KEY", "email").strip().lower()
        if match_key not in ("email", "username"):
            raise ConfigError(f"MATCH_KEY must be 'email' or 'username', got {match_key!r}")

        group_prefix = env.get("GROUP_PREFIX", "grafana-")
        if not group_prefix:
            raise ConfigError("GROUP_PREFIX must not be empty")

        raw_ratio = env.get("MAX_REMOVAL_RATIO", "0.5")
        try:
            max_removal_ratio = float(raw_ratio)
        except ValueError as exc:
            raise ConfigError(f"MAX_REMOVAL_RATIO must be a number, got {raw_ratio!r}") from exc
        if not 0.0 <= max_removal_ratio <= 1.0:
            raise ConfigError(f"MAX_REMOVAL_RATIO must be between 0 and 1, got {raw_ratio!r}")

        raw_role_suffixes = env.get("ROLE_SUFFIXES", "")
        role_suffixes = parse_role_suffixes(raw_role_suffixes) if raw_role_suffixes.strip() else frozenset(DEFAULT_ROLE_SUFFIXES)

        return cls(
            keycloak_url=env["KEYCLOAK_URL"].rstrip("/"),
            keycloak_realm=env["KEYCLOAK_REALM"],
            keycloak_client_id=env["KEYCLOAK_CLIENT_ID"],
            keycloak_client_secret=env["KEYCLOAK_CLIENT_SECRET"],
            grafana_url=env["GRAFANA_URL"].rstrip("/"),
            grafana_token=env["GRAFANA_TOKEN"],
            group_prefix=group_prefix,
            match_key=match_key,
            dry_run=parse_bool(env.get("DRY_RUN", "true"), "DRY_RUN"),
            max_removal_ratio=max_removal_ratio,
            log_level=env.get("LOG_LEVEL", "INFO"),
            role_suffixes=role_suffixes,
        )


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request with timeout and exponential-backoff retries.

    Retries connection errors, timeouts, and 5xx responses up to MAX_RETRIES
    times. 4xx responses are returned to the caller without retrying.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc: Exception | None = None
    last_resp: requests.Response | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log_event(logging.WARNING, "http_retry", method=method, url=url, attempt=attempt, delay_seconds=delay)
            time.sleep(delay)
        try:
            resp = session.request(method, url, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            last_resp = None
            continue
        if resp.status_code >= 500:
            last_exc = None
            last_resp = resp
            continue
        return resp
    if last_resp is not None:
        return last_resp
    assert last_exc is not None
    raise last_exc


class KeycloakClient:
    def __init__(self, url: str, realm: str, client_id: str, client_secret: str, session: requests.Session | None = None):
        self.base = url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._token: str | None = None
        # None = unknown, decided on first /children call (Keycloak >= 23
        # has the endpoint; older versions embed subGroups in /groups).
        self._children_endpoint_supported: bool | None = None

    @property
    def admin_base(self) -> str:
        return f"{self.base}/admin/realms/{self.realm}"

    def _fetch_token(self) -> None:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/realms/{self.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if resp.status_code != 200:
            raise AuthError(f"keycloak token request failed with status {resp.status_code}")
        self._token = resp.json()["access_token"]

    def _request(self, method: str, path: str, params: dict | None = None) -> requests.Response:
        if self._token is None:
            self._fetch_token()
        url = f"{self.admin_base}{path}"
        resp = request_with_retry(
            self.session, method, url, params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code == 401:
            # Token likely expired mid-run: re-issue once and retry.
            log_event(logging.INFO, "keycloak_token_refresh", path=path)
            self._fetch_token()
            resp = request_with_retry(
                self.session, method, url, params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code == 401:
                raise AuthError("keycloak request unauthorized after token refresh")
        if resp.status_code == 403:
            raise AuthError(
                f"keycloak returned 403 for {path}; "
                "service account needs 'view-users' and 'query-groups' roles"
            )
        return resp

    def _get_paginated(self, path: str, extra_params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        first = 0
        while True:
            params = {"first": first, "max": PAGE_SIZE}
            if extra_params:
                params.update(extra_params)
            resp = self._request("GET", path, params=params)
            if resp.status_code != 200:
                raise ApiError(f"keycloak GET {path} failed with status {resp.status_code}", status=resp.status_code)
            page = resp.json()
            items.extend(page)
            if len(page) < PAGE_SIZE:
                return items
            first += PAGE_SIZE

    def get_top_level_groups(self) -> list[dict]:
        return self._get_paginated("/groups")

    def get_children(self, group: dict) -> list[dict]:
        """Sub-groups of a group.

        Keycloak 23+ no longer populates subGroups in /groups responses, so
        query /groups/{id}/children; fall back to the embedded subGroups
        field for older versions (where /children does not exist and 404s).
        """
        if self._children_endpoint_supported is False:
            return group.get("subGroups") or []
        first = 0
        children: list[dict] = []
        while True:
            resp = self._request("GET", f"/groups/{group['id']}/children", params={"first": first, "max": PAGE_SIZE})
            if resp.status_code == 404:
                self._children_endpoint_supported = False
                return group.get("subGroups") or []
            if resp.status_code != 200:
                raise ApiError(
                    f"keycloak GET /groups/{group['id']}/children failed with status {resp.status_code}",
                    status=resp.status_code,
                )
            self._children_endpoint_supported = True
            page = resp.json()
            children.extend(page)
            if len(page) < PAGE_SIZE:
                return children
            first += PAGE_SIZE

    def collect_root_groups(self, prefix: str) -> list[dict]:
        """Groups whose name starts with the prefix, anywhere in the tree.

        Does not descend INTO a matched root group: its children are
        teams, handled separately by the sync.
        """
        roots: list[dict] = []
        queue = list(self.get_top_level_groups())
        while queue:
            group = queue.pop(0)
            if group.get("name", "").startswith(prefix):
                roots.append(group)
            else:
                queue.extend(self.get_children(group))
        return roots

    def get_group_members(self, group_id: str) -> list[dict]:
        """Direct members of a group (Keycloak does not include sub-group members)."""
        return self._get_paginated(f"/groups/{group_id}/members")


class GrafanaClient:
    def __init__(self, url: str, token: str, session: requests.Session | None = None):
        self.base = url.rstrip("/")
        self.token = token
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> requests.Response:
        resp = request_with_retry(
            self.session, method, f"{self.base}{path}", params=params, json=json,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if resp.status_code in (401, 403):
            raise AuthError(
                f"grafana returned {resp.status_code} for {path}; "
                "check the service account token and its org role"
            )
        return resp

    def find_team(self, name: str) -> dict | None:
        resp = self._request("GET", "/api/teams/search", params={"name": name})
        if resp.status_code != 200:
            raise ApiError(f"grafana team search failed with status {resp.status_code}", status=resp.status_code)
        for team in resp.json().get("teams", []):
            if team.get("name", "").lower() == name.lower():
                return team
        return None

    def create_team(self, name: str) -> int:
        resp = self._request("POST", "/api/teams", json={"name": name})
        if resp.status_code != 200:
            raise ApiError(f"grafana team creation for {name!r} failed with status {resp.status_code}", status=resp.status_code)
        return resp.json()["teamId"]

    def get_team_members(self, team_id: int) -> list[dict]:
        resp = self._request("GET", f"/api/teams/{team_id}/members")
        if resp.status_code != 200:
            raise ApiError(f"grafana team members fetch failed with status {resp.status_code}", status=resp.status_code)
        return resp.json()

    def lookup_user(self, login_or_email: str) -> dict | None:
        """Find an org user; None if they have never logged into Grafana yet."""
        resp = self._request("GET", "/api/users/lookup", params={"loginOrEmail": login_or_email})
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ApiError(f"grafana user lookup failed with status {resp.status_code}", status=resp.status_code)
        return resp.json()

    def add_team_member(self, team_id: int, user_id: int) -> None:
        resp = self._request("POST", f"/api/teams/{team_id}/members", json={"userId": user_id})
        if resp.status_code != 200:
            raise ApiError(f"grafana add member failed with status {resp.status_code}", status=resp.status_code)

    def remove_team_member(self, team_id: int, user_id: int) -> None:
        resp = self._request("DELETE", f"/api/teams/{team_id}/members/{user_id}")
        if resp.status_code != 200:
            raise ApiError(f"grafana remove member failed with status {resp.status_code}", status=resp.status_code)


@dataclass
class TeamResult:
    added: int = 0
    removed: int = 0
    pending: int = 0
    guard_triggered: bool = False


def desired_members(cfg: Config, kc: KeycloakClient, team_name: str, groups: list[dict]) -> set[str]:
    """Desired team membership: match keys of the groups' direct members."""
    desired: set[str] = set()
    for group in groups:
        for user in kc.get_group_members(group["id"]):
            if not user.get("enabled", True):
                continue
            raw = user.get("email") if cfg.match_key == "email" else user.get("username")
            if not raw or not raw.strip():
                log_event(
                    logging.WARNING, "member_missing_match_key",
                    team=team_name, group=group.get("name", ""), match_key=cfg.match_key,
                    target=user.get("username") or user.get("id") or "unknown",
                )
                continue
            desired.add(raw.strip().lower())
    return desired


def member_key(cfg: Config, member: dict) -> str:
    raw = member.get("email") if cfg.match_key == "email" else member.get("login")
    return (raw or "").strip().lower()


def sync_team(cfg: Config, kc: KeycloakClient, gf: GrafanaClient, team_name: str, groups: list[dict]) -> TeamResult:
    result = TeamResult()
    desired = desired_members(cfg, kc, team_name, groups)

    team = gf.find_team(team_name)
    team_id: int | None = None
    current_members: list[dict] = []
    if team is None:
        if not desired:
            # Avoid littering empty teams, e.g. a service group whose
            # members all live in its role sub-groups.
            log_event(logging.INFO, "empty_team_not_created", team=team_name)
            return result
        if cfg.dry_run:
            log_event(logging.INFO, "would_create_team", team=team_name)
        else:
            log_event(logging.INFO, "create_team", team=team_name)
            team_id = gf.create_team(team_name)
    else:
        team_id = team["id"]
        current_members = gf.get_team_members(team_id)

    current_by_key = {}
    for member in current_members:
        key = member_key(cfg, member)
        if key:
            current_by_key[key] = member

    to_add = sorted(desired - set(current_by_key))
    to_remove = sorted(set(current_by_key) - desired)

    if to_remove and current_members and (len(to_remove) / len(current_members)) > cfg.max_removal_ratio:
        log_event(
            logging.ERROR, "removal_guard_triggered",
            team=team_name, removals=len(to_remove), current_members=len(current_members),
            ratio=round(len(to_remove) / len(current_members), 3), limit=cfg.max_removal_ratio,
        )
        result.guard_triggered = True
        to_remove = []

    for key in to_add:
        user = gf.lookup_user(key)
        if user is None:
            result.pending += 1
            log_event(logging.INFO, "member_pending_first_login", team=team_name, target=key)
            continue
        if cfg.dry_run:
            log_event(logging.INFO, "would_add_member", team=team_name, target=key)
        else:
            log_event(logging.INFO, "add_member", team=team_name, target=key)
            gf.add_team_member(team_id, user["id"])
        result.added += 1

    for key in to_remove:
        member = current_by_key[key]
        if cfg.dry_run:
            log_event(logging.INFO, "would_remove_member", team=team_name, target=key)
        else:
            log_event(logging.INFO, "remove_member", team=team_name, target=key)
            gf.remove_team_member(team_id, member["userId"])
        result.removed += 1

    log_event(
        logging.INFO, "team_synced",
        team=team_name, added=result.added, removed=result.removed,
        pending=result.pending, dry_run=cfg.dry_run,
    )
    return result


def run_sync(cfg: Config, kc: KeycloakClient | None = None, gf: GrafanaClient | None = None) -> int:
    kc = kc or KeycloakClient(cfg.keycloak_url, cfg.keycloak_realm, cfg.keycloak_client_id, cfg.keycloak_client_secret)
    gf = gf or GrafanaClient(cfg.grafana_url, cfg.grafana_token)

    if cfg.dry_run:
        log_event(logging.INFO, "dry_run_enabled")

    roots = kc.collect_root_groups(cfg.group_prefix)

    # Children of a root group are services; each service's role
    # sub-groups (e.g. abc_adm, abc_editor, abc_viewer) are independent
    # teams named after the role group. Direct members of the service
    # group sync to a team named after the service. Same-named teams are
    # merged.
    teams: dict[str, list[dict]] = {}
    for root in roots:
        for service_group in kc.get_children(root):
            service_name = service_group.get("name", "").strip()
            if not service_name:
                continue
            teams.setdefault(service_name, []).append(service_group)
            for child in kc.get_children(service_group):
                child_name = child.get("name", "").strip()
                if not is_role_group(service_name, child_name, cfg.role_suffixes):
                    log_event(
                        logging.WARNING, "unknown_role_group_skipped",
                        service=service_name, group=child_name,
                        expected="|".join(f"{service_name}_{suffix}" for suffix in sorted(cfg.role_suffixes)),
                    )
                    continue
                teams.setdefault(child_name, []).append(child)

    if not teams:
        log_event(
            logging.WARNING, "no_managed_groups",
            prefix=cfg.group_prefix,
            detail="no team groups found under root groups matching the prefix; nothing changed",
        )
        return EXIT_OK

    exit_code = EXIT_OK
    total = TeamResult()
    failed_teams = 0
    for team_name in sorted(teams):
        try:
            result = sync_team(cfg, kc, gf, team_name, teams[team_name])
        except (ApiError, requests.RequestException) as exc:
            failed_teams += 1
            exit_code = EXIT_PARTIAL
            log_event(logging.ERROR, "team_sync_failed", team=team_name, error=str(exc))
            continue
        total.added += result.added
        total.removed += result.removed
        total.pending += result.pending
        if result.guard_triggered:
            exit_code = EXIT_PARTIAL

    log_event(
        logging.INFO, "sync_complete",
        teams=len(teams), failed_teams=failed_teams,
        added=total.added, removed=total.removed,
        pending_first_login=total.pending, dry_run=cfg.dry_run, exit_code=exit_code,
    )
    return exit_code


def main(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    setup_logging(env.get("LOG_LEVEL", "INFO"))
    try:
        cfg = Config.from_env(env)
    except ConfigError as exc:
        log_event(logging.ERROR, "config_error", error=str(exc))
        return EXIT_CONFIG
    try:
        return run_sync(cfg)
    except AuthError as exc:
        log_event(logging.ERROR, "auth_error", error=str(exc))
        return EXIT_CONFIG
    except (ApiError, requests.RequestException) as exc:
        log_event(logging.ERROR, "sync_failed", error=str(exc))
        return EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
