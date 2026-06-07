#!/usr/bin/env python3
"""
Sync Xray Cloud manual tests into OpenProject.

This script performs the full pipeline:
1. Export tests from Xray via GraphQL
2. Optionally write CSV/JSON exports
3. Create a parent OpenProject work package of type "Test" for each test
4. Create child work packages of type "Test Step" for each step and map the Xray step fields into OpenProject fields

Credentials are read from the environment:
- XRAY_CLIENT_ID
- XRAY_CLIENT_SECRET
- XRAY_BASE_URL (optional, default: https://xray.cloud.getxray.app)
- OPENPROJECT_URL (optional, can also be passed via --openproject-url)
- OPENPROJECT_API_TOKEN
- OPENPROJECT_AUTH_MODE (optional, default: bearer)
- OPENPROJECT_USERNAME (optional, only for basic auth; defaults to apikey)

Usage:
  python syncXrayToOpenProject.py --jql "project = ABC AND issuetype = Test" --target-project demo
  python syncXrayToOpenProject.py --jql "project = ABC AND issuetype = Test" --source-project-name "Lab OS" --target-project demo --dry-run
"""

import argparse
import csv
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin, urlparse

import requests

from env_loader import load_dotenv


DEFAULT_XRAY_BASE_URL = "https://xray.cloud.getxray.app"
DEFAULT_OPENPROJECT_URL = "https://openproject.example.com"
DEFAULT_XRAY_JQL = "issuetype IN ('Test', 'Test Execution', 'Test Plan', 'Test Set', 'Test Case')"


GET_TESTS_QUERY = """
query GetTests($jql: String!, $limit: Int!, $start: Int!) {
  getTests(jql: $jql, limit: $limit, start: $start) {
    total
    start
    limit
    results {
      issueId
      testType {
        name
        kind
      }
      jira(fields: ["key", "summary", "status", "project"])
      steps {
        id
        libStepId
        action
        data
        result
        attachments {
          id
          filename
        }
        customFields {
          id
          value
        }
      }
    }
  }
}
"""


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def response_text(response: requests.Response) -> str:
    text = normalize_text(response.text).strip()
    return text if text else "<empty response body>"


def openproject_connection_error(url: str, exc: Exception) -> RuntimeError:
    parsed = urlparse(url)
    host = parsed.hostname or url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = parsed.scheme or "http"
    message = (
        f"Cannot connect to OpenProject at {url}: {exc}. "
        f"Check the scheme ({scheme}), host ({host}), port ({port}), and network/VPN reachability."
    )
    if scheme == "http":
        message += " If OpenProject uses TLS, try https:// instead of http://."
    return RuntimeError(message)


def escape_table_cell(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


STEP_FIELD_ENV_VARS = {
    "description": "OPENPROJECT_STEP_DESCRIPTION_LABEL",
    "data": "OPENPROJECT_STEP_DATA_LABEL",
    "expected": "OPENPROJECT_STEP_EXPECTED_LABEL",
    "result": "OPENPROJECT_STEP_RESULT_LABEL",
}


def step_field_labels() -> Dict[str, str]:
    return {
        "description": normalize_text(os.getenv(STEP_FIELD_ENV_VARS["description"], "Test Step Description")).strip(),
        "data": normalize_text(os.getenv(STEP_FIELD_ENV_VARS["data"], "Test Step Data")).strip(),
        "expected": normalize_text(os.getenv(STEP_FIELD_ENV_VARS["expected"], "Test Step Expected")).strip(),
        "result": normalize_text(os.getenv(STEP_FIELD_ENV_VARS["result"], "Test Step Result")).strip(),
    }


def resolve_custom_field_key_from_schema(schema: Dict[str, Any], selector: str) -> Optional[str]:
    if re.fullmatch(r"customField\d+", selector):
        field = schema.get(selector)
        if not isinstance(field, dict):
            print(f"Warning: Custom field {selector!r} was not present on the Test Step schema", file=sys.stderr)
            return None
        if normalize_text(field.get("location")).strip() == "_links":
            print(
                f"Warning: Custom field {selector!r} is linked and may not accept the step value",
                file=sys.stderr,
            )
            return None
        return selector

    wanted = normalize_text(selector).strip().lower()
    matches: List[str] = []

    for key, value in schema.items():
        if not re.fullmatch(r"customField\d+", key):
            continue
        if not isinstance(value, dict):
            continue
        if normalize_text(value.get("name")).strip().lower() == wanted:
            matches.append(key)

    if not matches:
        print(f"Warning: Custom field {selector!r} was not found on the Test Step schema", file=sys.stderr)
        return None

    if len(matches) > 1:
        print(
            f"Warning: Custom field label {selector!r} is ambiguous on the Test Step schema: {', '.join(matches)}",
            file=sys.stderr,
        )
        return None

    field = schema[matches[0]]
    if isinstance(field, dict) and normalize_text(field.get("location")).strip() == "_links":
        print(
            f"Warning: Custom field {selector!r} resolves to a linked field and may not accept the step value",
            file=sys.stderr,
        )
        return None

    return matches[0]


def resolve_step_custom_field_keys(
    schema: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    labels = step_field_labels()
    return {
        field_name: resolve_custom_field_key_from_schema(schema, label)
        for field_name, label in labels.items()
    }


def project_id_from_href(project_link: str) -> str:
    match = re.search(r"/projects/(\d+)", project_link)
    if not match:
        raise RuntimeError(f"Cannot extract project id from link: {project_link!r}")
    return match.group(1)


def type_id_from_href(type_href: str) -> str:
    match = re.search(r"/types/(\d+)", type_href)
    if not match:
        raise RuntimeError(f"Cannot extract type id from link: {type_href!r}")
    return match.group(1)


def work_package_schema_url(openproject_url: str, project_link: str, type_href: str) -> str:
    project_id = project_id_from_href(project_link)
    type_id = type_id_from_href(type_href)
    identifier = f"{project_id}-{type_id}"
    return f"{openproject_url.rstrip('/')}/api/v3/work_packages/schemas/{identifier}"


def work_package_form_url(openproject_url: str) -> str:
    return f"{openproject_url.rstrip('/')}/api/v3/work_packages/form"


def fetch_work_package_form_schema(
    session: requests.Session,
    openproject_url: str,
    project_link: str,
    type_href: str,
    auth_mode: str,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "_links": {
            "project": {"href": project_link},
            "type": {"href": type_href},
        }
    }
    response = api_post(session, work_package_form_url(openproject_url), payload, auth_mode, username)
    embedded = response.get("_embedded") or {}
    schema = embedded.get("schema")
    if not isinstance(schema, dict):
        raise RuntimeError("Unexpected OpenProject work package form response")
    return schema


def custom_field_value(field_schema: Dict[str, Any], value: str) -> Any:
    field_type = normalize_text(field_schema.get("type"))
    if field_type == "Formattable":
        return {"format": "markdown", "raw": value, "html": ""}
    return value


def step_custom_fields(
    step: Dict[str, Any],
    schema: Dict[str, Any],
    field_keys: Dict[str, Optional[str]],
    leave_result_empty: bool,
) -> Dict[str, Any]:
    values = {
        "description": normalize_text(step.get("action")),
        "data": normalize_text(step.get("data")),
        "expected": normalize_text(step.get("result")),
        "result": "" if leave_result_empty else normalize_text(step.get("result")),
    }

    custom_fields: Dict[str, Any] = {}
    for field_name, field_key in field_keys.items():
        if not field_key:
            continue
        field_schema = schema.get(field_key)
        if not isinstance(field_schema, dict):
            continue
        value = values[field_name]
        if not value:
            continue
        custom_fields[field_key] = custom_field_value(field_schema, value)

    return custom_fields


def flatten_jira_fields(jira_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    jira_data = jira_data or {}
    project = jira_data.get("project") or {}
    status = jira_data.get("status") or {}
    return {
        "test_key": jira_data.get("key"),
        "test_summary": jira_data.get("summary"),
        "test_status": status.get("name") if isinstance(status, dict) else status,
        "project_key": project.get("key") if isinstance(project, dict) else None,
        "project_name": project.get("name") if isinstance(project, dict) else None,
    }


def source_project_name(test: Dict[str, Any]) -> str:
    jira = test.get("jira") or {}
    project = jira.get("project") or {}
    if isinstance(project, dict):
        return normalize_text(project.get("name"))
    return ""


def test_key(test: Dict[str, Any]) -> str:
    jira = test.get("jira") or {}
    key = normalize_text(jira.get("key"))
    if not key:
        raise RuntimeError("Test item is missing jira.key")
    return key


def test_summary(test: Dict[str, Any]) -> str:
    jira = test.get("jira") or {}
    return normalize_text(jira.get("summary"))


def build_subject(prefix: str, summary: str) -> str:
    subject = f"{prefix} - {summary}" if summary else prefix
    return subject[:255]


def authenticate_xray(base_url: str, client_id: str, client_secret: str) -> str:
    url = f"{base_url.rstrip('/')}/api/v2/authenticate"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={"client_id": client_id, "client_secret": client_secret},
        timeout=60,
    )
    if not response.ok:
        if response.status_code == 401:
            raise RuntimeError(
                "Xray authentication failed with 401 Unauthorized. "
                "Check XRAY_CLIENT_ID, XRAY_CLIENT_SECRET, and XRAY_BASE_URL for the correct tenant/region. "
                f"Response: {response_text(response)}"
            )
        raise RuntimeError(f"Xray authentication failed with {response.status_code}: {response_text(response)}")
    token = response.json()
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Unexpected authentication response: {token!r}")
    return token


def xray_graphql(base_url: str, token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v2/graphql"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(json.dumps(payload["errors"], indent=2, ensure_ascii=False))
    return payload["data"]


def fetch_all_tests(base_url: str, token: str, jql: str, page_size: int = 100) -> List[Dict[str, Any]]:
    all_tests = []
    start = 0
    total = None

    while total is None or start < total:
        data = xray_graphql(
            base_url=base_url,
            token=token,
            query=GET_TESTS_QUERY,
            variables={"jql": jql, "limit": page_size, "start": start},
        )

        result = data["getTests"]
        total = result["total"]
        tests = result["results"] or []
        all_tests.extend(tests)

        print(f"Fetched {len(all_tests)} / {total} tests...", file=sys.stderr)
        start += page_size

    return all_tests


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "test_issue_id",
        "test_key",
        "test_summary",
        "test_status",
        "project_key",
        "project_name",
        "test_type_name",
        "test_type_kind",
        "step_index",
        "step_id",
        "library_step_id",
        "step_action",
        "step_data",
        "step_expected_result",
        "step_attachments_json",
        "step_customfields_json",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: str, tests: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(tests, file, indent=2, ensure_ascii=False)


def extract_rows_from_test(test: Dict[str, Any]) -> List[Dict[str, Any]]:
    jira_fields = flatten_jira_fields(test.get("jira"))
    test_type = test.get("testType") or {}
    steps = test.get("steps") or []
    rows: List[Dict[str, Any]] = []

    for index, step in enumerate(steps, start=1):
        rows.append(
            {
                "test_issue_id": test.get("issueId"),
                "test_key": jira_fields["test_key"],
                "test_summary": jira_fields["test_summary"],
                "test_status": jira_fields["test_status"],
                "project_key": jira_fields["project_key"],
                "project_name": jira_fields["project_name"],
                "test_type_name": test_type.get("name"),
                "test_type_kind": test_type.get("kind"),
                "step_index": index,
                "step_id": step.get("id"),
                "library_step_id": step.get("libStepId"),
                "step_action": step.get("action"),
                "step_data": step.get("data"),
                "step_expected_result": step.get("result"),
                "step_attachments_json": json.dumps(step.get("attachments") or [], ensure_ascii=False) if step.get("attachments") else "",
                "step_customfields_json": json.dumps(step.get("customFields") or [], ensure_ascii=False) if step.get("customFields") else "",
            }
        )

    if not steps:
        rows.append(
            {
                "test_issue_id": test.get("issueId"),
                "test_key": jira_fields["test_key"],
                "test_summary": jira_fields["test_summary"],
                "test_status": jira_fields["test_status"],
                "project_key": jira_fields["project_key"],
                "project_name": jira_fields["project_name"],
                "test_type_name": test_type.get("name"),
                "test_type_kind": test_type.get("kind"),
                "step_index": None,
                "step_id": None,
                "library_step_id": None,
                "step_action": None,
                "step_data": None,
                "step_expected_result": None,
                "step_attachments_json": "",
                "step_customfields_json": "",
            }
        )

    return rows


def session_with_auth(api_token: str, auth_mode: str, username: Optional[str] = None) -> requests.Session:
    session = requests.Session()
    if auth_mode == "basic":
        session.auth = (username or "apikey", api_token)
    else:
        session.headers.update({"Authorization": f"Bearer {api_token}"})
    session.headers.update({"Accept": "application/hal+json", "Content-Type": "application/json"})
    return session


def raise_for_openproject_error(
    response: requests.Response,
    auth_mode: str,
    username: Optional[str] = None,
) -> None:
    hint = ""
    if response.status_code == 401:
        if auth_mode == "bearer":
            hint = " If your OpenProject token is an API token, try --auth-mode basic --username apikey."
        elif auth_mode == "basic":
            hint = f" Check the username ({username or 'apikey'}) and token for this OpenProject instance."

    try:
        payload = response.json()
    except Exception:
        message = normalize_text(response.text).strip() or response.reason or "<no response body>"
        raise RuntimeError(f"OpenProject API error {response.status_code}: {message}{hint}")

    message = payload.get("message") or payload.get("error") or response.text
    if response.status_code == 401:
        message = f"{message}{hint}"
    raise RuntimeError(f"OpenProject API error {response.status_code}: {message}")


def api_get(session: requests.Session, url: str, auth_mode: str, username: Optional[str] = None) -> Dict[str, Any]:
    try:
        response = session.get(url, timeout=60)
    except requests.exceptions.ConnectionError as exc:
        raise openproject_connection_error(url, exc) from exc
    except requests.exceptions.Timeout as exc:
        raise openproject_connection_error(url, exc) from exc
    if response.ok:
        return response.json()
    raise_for_openproject_error(response, auth_mode, username)


def api_post(
    session: requests.Session,
    url: str,
    payload: Dict[str, Any],
    auth_mode: str,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        response = session.post(url, json=payload, timeout=60)
    except requests.exceptions.ConnectionError as exc:
        raise openproject_connection_error(url, exc) from exc
    except requests.exceptions.Timeout as exc:
        raise openproject_connection_error(url, exc) from exc
    if response.ok:
        return response.json()
    raise_for_openproject_error(response, auth_mode, username)


def project_href(project_ref: str) -> str:
    return f"/api/v3/projects/{quote(project_ref, safe='')}"


def projects_collection_url(openproject_url: str) -> str:
    return f"{openproject_url.rstrip('/')}/api/v3/projects"


def absolute_url(openproject_url: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(openproject_url.rstrip("/") + "/", href.lstrip("/"))


def list_openproject_projects(
    session: requests.Session,
    openproject_url: str,
    auth_mode: str,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    url = projects_collection_url(openproject_url)
    projects: List[Dict[str, Any]] = []

    while url:
        try:
            response = session.get(url, timeout=60)
        except requests.exceptions.ConnectionError as exc:
            raise openproject_connection_error(url, exc) from exc
        except requests.exceptions.Timeout as exc:
            raise openproject_connection_error(url, exc) from exc
        if not response.ok:
            raise_for_openproject_error(response, auth_mode, username)

        payload = response.json()
        elements = (payload.get("_embedded") or {}).get("elements") or []
        if not isinstance(elements, list):
            raise RuntimeError("Unexpected OpenProject projects response")
        projects.extend(elements)

        next_link = (payload.get("_links") or {}).get("nextByOffset") or {}
        href = next_link.get("href")
        url = absolute_url(openproject_url, href) if href else None

    return projects


def prompt_select_option(title: str, options: List[str]) -> int:
    if not options:
        raise RuntimeError(f"No options available for {title}")
    if len(options) == 1:
        return 1
    if not sys.stdin.isatty():
        raise RuntimeError(f"{title} is required but not provided and no interactive terminal is available")

    print(f"Select {title}:", file=sys.stderr)
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}", file=sys.stderr)

    while True:
        choice = input(f"Enter {title} number [1-{len(options)}]: ").strip()
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(options):
                return index
        print("Invalid selection.", file=sys.stderr)


def prompt_source_project_name(tests: List[Dict[str, Any]], explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit

    names = sorted({source_project_name(test) for test in tests if source_project_name(test)})
    if not names:
        return None
    return names[prompt_select_option("source project", names) - 1]


def prompt_target_project_name(
    session: requests.Session,
    openproject_url: str,
    auth_mode: str,
    username: Optional[str] = None,
) -> str:
    projects = list_openproject_projects(session, openproject_url, auth_mode, username)
    labels = []
    refs = []
    for project in projects:
        identifier = normalize_text(project.get("identifier"))
        name = normalize_text(project.get("name"))
        ref = identifier or name
        if not ref:
            continue
        if identifier and name and identifier != name:
            labels.append(f"{name} ({identifier})")
        else:
            labels.append(ref)
        refs.append(ref)
    if not labels:
        raise RuntimeError("No OpenProject projects available to select")
    selected_index = prompt_select_option("destination project", labels)
    return refs[selected_index - 1]


def resolve_project_href(
    session: requests.Session,
    openproject_url: str,
    project_ref: str,
    auth_mode: str,
    username: Optional[str] = None,
) -> str:
    if project_ref.isdigit():
        return project_href(project_ref)

    url = projects_collection_url(openproject_url)
    while url:
        try:
            response = session.get(url, timeout=60)
        except requests.exceptions.ConnectionError as exc:
            raise openproject_connection_error(url, exc) from exc
        except requests.exceptions.Timeout as exc:
            raise openproject_connection_error(url, exc) from exc
        if not response.ok:
            raise_for_openproject_error(response, auth_mode, username)

        payload = response.json()
        elements = (payload.get("_embedded") or {}).get("elements") or []
        if not isinstance(elements, list):
            raise RuntimeError("Unexpected OpenProject projects response")

        for project in elements:
            identifier = normalize_text(project.get("identifier"))
            name = normalize_text(project.get("name"))
            if project_ref.lower() in {identifier.lower(), name.lower()}:
                self_link = (project.get("_links") or {}).get("self") or {}
                href = self_link.get("href")
                if href:
                    return href

        next_link = (payload.get("_links") or {}).get("nextByOffset") or {}
        href = next_link.get("href")
        url = absolute_url(openproject_url, href) if href else None

    raise RuntimeError(f"Project not found in OpenProject: {project_ref!r}")


def list_project_types(
    session: requests.Session,
    openproject_url: str,
    project_ref: str,
    auth_mode: str,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    project_link = resolve_project_href(session, openproject_url, project_ref, auth_mode, username)
    url = f"{openproject_url.rstrip('/')}{project_link}/types"
    payload = api_get(session, url, auth_mode, username)
    embedded = payload.get("_embedded") or {}
    elements = embedded.get("elements") or []
    if not isinstance(elements, list):
        raise RuntimeError("Unexpected OpenProject types response")
    return elements


def find_type_href(types: Iterable[Dict[str, Any]], type_name: str) -> str:
    for item in types:
        if normalize_text(item.get("name")).lower() == type_name.lower():
            self_link = (item.get("_links") or {}).get("self") or {}
            href = self_link.get("href")
            if href:
                return href
    raise RuntimeError(f"Type not available in target project: {type_name!r}")


def create_work_package(
    session: requests.Session,
    openproject_url: str,
    project_link: str,
    type_href: str,
    subject: str,
    description: str,
    custom_fields: Optional[Dict[str, Any]],
    auth_mode: str,
    username: Optional[str] = None,
    parent_href: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "subject": subject,
        "_links": {
            "project": {"href": project_link},
            "type": {"href": type_href},
        },
    }

    if description:
        payload["description"] = {"format": "markdown", "raw": description}

    if custom_fields:
        payload.update(custom_fields)

    if parent_href:
        payload["_links"]["parent"] = {"href": parent_href}

    url = f"{openproject_url.rstrip('/')}{project_link}/work_packages"
    return api_post(session, url, payload, auth_mode, username)


def tests_to_import(tests: List[Dict[str, Any]], source_project_filter: Optional[str]) -> List[Dict[str, Any]]:
    if not source_project_filter:
        return tests

    filtered = [
        test for test in tests if source_project_name(test).lower() == source_project_filter.lower()
    ]
    if not filtered:
        raise RuntimeError(f"No tests found for source project name: {source_project_filter!r}")
    return filtered


def import_tests(
    session: requests.Session,
    openproject_url: str,
    target_project: str,
    tests: List[Dict[str, Any]],
    dry_run: bool,
    auth_mode: str,
    username: Optional[str] = None,
    leave_step_result_empty: bool = False,
) -> None:
    project_link = resolve_project_href(session, openproject_url, target_project, auth_mode, username)
    types = list_project_types(session, openproject_url, target_project, auth_mode, username)
    test_type_href = find_type_href(types, "Test")
    step_type_href = find_type_href(types, "Test Step")
    step_form_schema = fetch_work_package_form_schema(session, openproject_url, project_link, step_type_href, auth_mode, username)
    step_field_keys = resolve_step_custom_field_keys(step_form_schema)

    created_tests = 0
    created_steps = 0

    for test in tests:
        key = test_key(test)
        summary = test_summary(test)
        parent_subject = build_subject(key, summary)
        steps = test.get("steps") or []

        parent_description = "\n".join(
            [
                f"# {escape_table_cell(key)}",
                "",
                f"Source project: {escape_table_cell(source_project_name(test))}",
                f"Summary: {escape_table_cell(summary)}",
            ]
        )

        print(f"Creating Test: {parent_subject}", file=sys.stderr)
        if dry_run:
            parent_result = {"_links": {"self": {"href": "DRY_RUN_PARENT"}}}
        else:
            parent_result = create_work_package(
                session=session,
                openproject_url=openproject_url,
                project_link=project_link,
                type_href=test_type_href,
                subject=parent_subject,
                description=parent_description,
                custom_fields=None,
                auth_mode=auth_mode,
                username=username,
            )
        created_tests += 1

        parent_href = ((parent_result.get("_links") or {}).get("self") or {}).get("href")
        if not dry_run and not parent_href:
            raise RuntimeError(f"Failed to resolve created parent work package for {key!r}")

        for index, step in enumerate(steps, start=1):
            step_subject = f"{key} - Step {index}"
            step_custom_field_values = step_custom_fields(
                step,
                step_form_schema,
                step_field_keys,
                leave_step_result_empty,
            )

            print(f"  Creating Test Step: {step_subject}", file=sys.stderr)
            if not dry_run:
                create_work_package(
                    session=session,
                    openproject_url=openproject_url,
                    project_link=project_link,
                    type_href=step_type_href,
                    subject=step_subject,
                    description="",
                    custom_fields=step_custom_field_values or None,
                    auth_mode=auth_mode,
                    username=username,
                    parent_href=parent_href,
                )
            created_steps += 1

    print(f"Done. Tests created: {created_tests}", file=sys.stderr)
    print(f"Steps created: {created_steps}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Xray tests and import them into OpenProject.")
    parser.add_argument(
        "--jql",
        default=DEFAULT_XRAY_JQL,
        help=f'JQL selecting the Xray Test issues. Default: {DEFAULT_XRAY_JQL!r}',
    )
    parser.add_argument("--source-project-name", default=None, help="Optional source project name filter from the exported tests.")
    parser.add_argument("--target-project", default=None, help="OpenProject target project identifier or numeric ID. If omitted, you will be prompted.")
    parser.add_argument("--openproject-url", default=None, help=f"OpenProject base URL. Default: OPENPROJECT_URL or {DEFAULT_OPENPROJECT_URL}")
    parser.add_argument("--csv", default="xray_test_steps.csv", help="CSV export path. Default: xray_test_steps.csv")
    parser.add_argument("--json", default="xray_test_steps.json", help="JSON export path. Default: xray_test_steps.json")
    parser.add_argument("--page-size", type=int, default=100, help="Xray page size. Xray Cloud max is 100. Default: 100")
    parser.add_argument("--auth-mode", choices=["bearer", "basic"], default=None, help="OpenProject auth mode. Default: OPENPROJECT_AUTH_MODE or bearer")
    parser.add_argument("--username", default=None, help="OpenProject username for basic auth. Default: OPENPROJECT_USERNAME or apikey")
    parser.add_argument(
        "--leave-step-result-empty",
        action="store_true",
        help="Leave the OpenProject field 'Test Step Result' empty instead of copying Xray result.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without creating anything.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()

    if args.page_size < 1 or args.page_size > 100:
        print("Error: --page-size must be between 1 and 100.", file=sys.stderr)
        return 2

    xray_base_url = os.getenv("XRAY_BASE_URL", DEFAULT_XRAY_BASE_URL)
    openproject_url = args.openproject_url or os.getenv("OPENPROJECT_URL", DEFAULT_OPENPROJECT_URL)
    auth_mode = args.auth_mode or os.getenv("OPENPROJECT_AUTH_MODE", "bearer")
    username = args.username or os.getenv("OPENPROJECT_USERNAME", "apikey")

    client_id = require_env("XRAY_CLIENT_ID")
    client_secret = require_env("XRAY_CLIENT_SECRET")
    api_token = require_env("OPENPROJECT_API_TOKEN")
    session = session_with_auth(api_token, auth_mode, username)
    target_project = args.target_project or prompt_target_project_name(session, openproject_url, auth_mode, username)

    print("Authenticating against Xray Cloud...", file=sys.stderr)
    token = authenticate_xray(xray_base_url, client_id, client_secret)

    print(f"Exporting tests using JQL: {args.jql}", file=sys.stderr)
    tests = fetch_all_tests(xray_base_url, token, args.jql, page_size=args.page_size)
    source_project_name_filter = prompt_source_project_name(tests, args.source_project_name)
    tests = tests_to_import(tests, source_project_name_filter)

    rows: List[Dict[str, Any]] = []
    for test in tests:
        rows.extend(extract_rows_from_test(test))

    write_csv(args.csv, rows)
    write_json(args.json, tests)

    print(f"CSV: {args.csv}", file=sys.stderr)
    print(f"JSON: {args.json}", file=sys.stderr)
    print(
        f"Connecting to OpenProject: {openproject_url} (auth mode: {auth_mode}, username: {username if auth_mode == 'basic' else 'n/a'})",
        file=sys.stderr,
    )

    import_tests(
        session,
        openproject_url,
        target_project,
        tests,
        args.dry_run,
        auth_mode,
        username,
        args.leave_step_result_empty,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
