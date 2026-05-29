# XRay to OpenProject Converter

Sync Xray Cloud manual tests into OpenProject.

This tool exports manual tests from Xray, writes the test data to CSV and JSON, and then recreates the tests in OpenProject as a parent work package with child work packages for each step. It is meant to keep test cases aligned between both systems with a single command.

## Setup

```powershell
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials. The script loads `.env` from the current directory automatically.

## Full Sync

```powershell
python XRay2OpenProjectConverter.py --source-project-name "Lab OS" --target-project demo
```

Useful options:
- `--jql` defaults to `issuetype IN ('Test', 'Test Execution', 'Test Plan', 'Test Set', 'Test Case')`
- Omit `--source-project-name` and/or `--target-project` to select them interactively
- `--csv xray_test_steps.csv` and `--json xray_test_steps.json` to change export paths
- `--dry-run` to print actions without creating anything
- `--page-size 100` to control Xray pagination
- `--openproject-url https://your-openproject.example.com` to override the target instance
- `--auth-mode basic --username apikey` if your OpenProject instance uses basic auth

## Environment Variables

- `XRAY_BASE_URL`
- `XRAY_CLIENT_ID`
- `XRAY_CLIENT_SECRET`
- `OPENPROJECT_URL`
- `OPENPROJECT_API_TOKEN`
- `OPENPROJECT_AUTH_MODE`
- `OPENPROJECT_USERNAME`
- `OPENPROJECT_RESULT_COLUMN_LABEL` (optional, label or `customFieldN` key for the Test Step result table field)

## Behavior

- Exports tests from Xray via GraphQL
- Writes CSV and JSON exports
- Creates one parent OpenProject work package of type `Test` per test
- Creates child work packages of type `Test Step` for each step
- Writes the result table into `description` by default
- Writes the result table into the configured Test Step custom field when `OPENPROJECT_RESULT_COLUMN_LABEL` is set

## Notes

- The target OpenProject project must already have the types `Test` and `Test Step` available.
- The step table includes `Action`, `Data`, `Expected`, `Result`, `Result 1`, `Date/Version`, `Tester`, `Result 2`, `Date/Version`, and `Tester` columns.
- Set `OPENPROJECT_RESULT_COLUMN_LABEL` to the custom field label or exact `customFieldN` key used on `Test Step` if you want to store the table in a custom field.
- Leave it unset to store the table in the built-in `description` field.
