#!/usr/bin/env python3
"""
Sync Xray Cloud manual tests into OpenProject.

This script performs the full pipeline:
1. Export tests from Xray via GraphQL
2. Optionally write CSV/JSON exports
3. Create a parent OpenProject work package of type "Test" for each test
4. Create child work packages of type "Test Step" for each step

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
import sys
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin

import requests

from env_loader import load_dotenv


DEFAULT_XRAY_BASE_URL = "https://xray.cloud.getxray.app"
DEFAULT_OPENPROJECT_URL = "https://openproject.example.com"


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


def escape_table_cell(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


def step_description_table(step: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "| action | data | expected results | result |",
            "| --- | --- | --- | --- |",
            f"| {escape_table_cell(step.get('action'))} | {escape_table_cell(step.get('data'))} | {escape_table_cell(step.get('result'))} |  |",
        ]
    )


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
    response.raise_for_status()
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


def raise_for_openproject_error(response: requests.Response) -> None:
    try:
        payload = response.json()
    except Exception:
        response.raise_for_status()
        return

    message = payload.get("message") or payload.get("error") or response.text
    raise RuntimeError(f"OpenProject API error {response.status_code}: {message}")


def api_get(session: requests.Session, url: str) -> Dict[str, Any]:
    response = session.get(url, timeout=60)
    if response.ok:
        return response.json()
    raise_for_openproject_error(response)


def api_post(session: requests.Session, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = session.post(url, json=payload, timeout=60)
    if response.ok:
        return response.json()
    raise_for_openproject_error(response)


def project_href(project_ref: str) -> str:
    return f"/api/v3/projects/{quote(project_ref, safe='')}"


def projects_collection_url(openproject_url: str) -> str:
    return f"{openproject_url.rstrip('/')}/api/v3/projects"


def absolute_url(openproject_url: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(openproject_url.rstrip("/") + "/", href.lstrip("/"))


def resolve_project_href(session: requests.Session, openproject_url: str, project_ref: str) -> str:
    if project_ref.isdigit():
        return project_href(project_ref)

    url = projects_collection_url(openproject_url)
    while url:
        response = session.get(url, timeout=60)
        if not response.ok:
            raise_for_openproject_error(response)

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


def list_project_types(session: requests.Session, openproject_url: str, project_ref: str) -> List[Dict[str, Any]]:
    project_link = resolve_project_href(session, openproject_url, project_ref)
    url = f"{openproject_url.rstrip('/')}{project_link}/types"
    payload = api_get(session, url)
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
    parent_href: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "subject": subject,
        "description": {"format": "markdown", "raw": description},
        "_links": {
            "project": {"href": project_link},
            "type": {"href": type_href},
        },
    }

    if parent_href:
        payload["_links"]["parent"] = {"href": parent_href}

    url = f"{openproject_url.rstrip('/')}{project_link}/work_packages"
    return api_post(session, url, payload)


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
) -> None:
    project_link = resolve_project_href(session, openproject_url, target_project)
    types = list_project_types(session, openproject_url, target_project)
    test_type_href = find_type_href(types, "Test")
    step_type_href = find_type_href(types, "Test Step")

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
            )
        created_tests += 1

        parent_href = ((parent_result.get("_links") or {}).get("self") or {}).get("href")
        if not dry_run and not parent_href:
            raise RuntimeError(f"Failed to resolve created parent work package for {key!r}")

        for index, step in enumerate(steps, start=1):
            step_subject = f"{key} - Step {index}"
            step_description = step_description_table(step)

            print(f"  Creating Test Step: {step_subject}", file=sys.stderr)
            if not dry_run:
                create_work_package(
                    session=session,
                    openproject_url=openproject_url,
                    project_link=project_link,
                    type_href=step_type_href,
                    subject=step_subject,
                    description=step_description,
                    parent_href=parent_href,
                )
            created_steps += 1

    print(f"Done. Tests created: {created_tests}", file=sys.stderr)
    print(f"Steps created: {created_steps}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Xray tests and import them into OpenProject.")
    parser.add_argument("--jql", required=True, help='JQL selecting the Xray Test issues, e.g. "project = ABC AND issuetype = Test"')
    parser.add_argument("--source-project-name", default=None, help="Optional source project name filter from the exported tests.")
    parser.add_argument("--target-project", required=True, help="OpenProject target project identifier or numeric ID.")
    parser.add_argument("--openproject-url", default=None, help=f"OpenProject base URL. Default: OPENPROJECT_URL or {DEFAULT_OPENPROJECT_URL}")
    parser.add_argument("--csv", default="xray_test_steps.csv", help="CSV export path. Default: xray_test_steps.csv")
    parser.add_argument("--json", default="xray_test_steps.json", help="JSON export path. Default: xray_test_steps.json")
    parser.add_argument("--page-size", type=int, default=100, help="Xray page size. Xray Cloud max is 100. Default: 100")
    parser.add_argument("--auth-mode", choices=["bearer", "basic"], default=None, help="OpenProject auth mode. Default: OPENPROJECT_AUTH_MODE or bearer")
    parser.add_argument("--username", default=None, help="OpenProject username for basic auth. Default: OPENPROJECT_USERNAME or apikey")
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

    print("Authenticating against Xray Cloud...", file=sys.stderr)
    token = authenticate_xray(xray_base_url, client_id, client_secret)

    print(f"Exporting tests using JQL: {args.jql}", file=sys.stderr)
    tests = fetch_all_tests(xray_base_url, token, args.jql, page_size=args.page_size)
    tests = tests_to_import(tests, args.source_project_name)

    rows: List[Dict[str, Any]] = []
    for test in tests:
        rows.extend(extract_rows_from_test(test))

    write_csv(args.csv, rows)
    write_json(args.json, tests)

    print(f"CSV: {args.csv}", file=sys.stderr)
    print(f"JSON: {args.json}", file=sys.stderr)

    session = session_with_auth(api_token, auth_mode, username)
    import_tests(session, openproject_url, args.target_project, tests, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
