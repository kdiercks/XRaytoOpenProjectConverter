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
python XRay2OpenProjectConverter.py --jql "project = 'Lab OS' AND issuetype = Test" --source-project-name "Lab OS" --target-project demo
```

Useful options:
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

## Behavior

- Exports tests from Xray via GraphQL
- Writes CSV and JSON exports
- Creates one parent OpenProject work package of type `Test` per test
- Creates child work packages of type `Test Step` for each step
- Writes a Markdown table into each step description

## Notes

- The target OpenProject project must already have the types `Test` and `Test Step` available.
- The step table includes `Action`, `Data`, `Expected Result`, `Result 1`, and `Result 2` columns.
