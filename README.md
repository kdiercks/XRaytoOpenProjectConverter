# XRay to OpenProject Converter

Sync Xray Cloud manual tests into OpenProject.

This tool exports manual tests from Xray, writes the test data to CSV and JSON, and then recreates the tests in OpenProject as a parent work package with child work packages for each step. Step data is written into dedicated OpenProject fields instead of a table.

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
- `--leave-step-result-empty` to keep `Test Step Result` empty

## Environment Variables

- `XRAY_BASE_URL`
- `XRAY_CLIENT_ID`
- `XRAY_CLIENT_SECRET`
- `OPENPROJECT_URL`
- `OPENPROJECT_API_TOKEN`
- `OPENPROJECT_AUTH_MODE`
- `OPENPROJECT_USERNAME`
- `OPENPROJECT_STEP_DESCRIPTION_LABEL`
- `OPENPROJECT_STEP_DATA_LABEL`
- `OPENPROJECT_STEP_EXPECTED_LABEL`
- `OPENPROJECT_STEP_RESULT_LABEL`

## Behavior

- Exports tests from Xray via GraphQL
- Writes CSV and JSON exports
- Creates one parent OpenProject work package of type `Test` per test
- Creates child work packages of type `Test Step` for each step
- Maps Xray step `action`, `data`, and `result` into the OpenProject fields configured via the four `OPENPROJECT_STEP_*_LABEL` variables

## Notes

- The target OpenProject project must already have the types `Test` and `Test Step` available.
- Default field labels are `Test Step Description`, `Test Step Data`, `Test Step Expected`, and `Test Step Result`.
