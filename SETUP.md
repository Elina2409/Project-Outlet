# Replicating this tracker with your own GitHub (+ optional GCP)

This guide takes you from nothing to a working Nordic sport-retail SKU
tracker: scrapers for sportoutlet.no and xxl.no (whole catalog + per
category), run on GitHub's free runners, with results committed to the
repo as CSV. Written for a colleague setting up her **own GitHub
account** (and optionally her own GCP project as a debugging fallback).

There are two independent tiers:

- **Tier 1 — GitHub-only tracker, ~10 minutes.** The scrapers run on
  GitHub-hosted runners via a button click (or a cron you enable) and
  commit `data/sku_counts.csv` to the repo. Because the CSV lives in the
  repo, any Claude Code session opened on the repo can read it, chart
  it, and answer questions about it directly.
- **Tier 2 — optional GCP Cloud Run fallback.** A dispatch-button
  workflow that builds the same code into a container, runs it as a
  Cloud Run Job, and prints the logs into the Actions run. Use it when
  you need lots of trial-and-error runs or a different egress network
  than GitHub's — not for data persistence (Cloud Run containers are
  ephemeral; the committed CSV stays on the Tier 1 path).

---

## Tier 1: GitHub-only tracker

### 1. Create the repo

Create a new GitHub repository (private is fine) and copy these files in:

```
main.py
requirements.txt
scrapers/            # the whole package
debug_page.py
.gitignore
.github/workflows/scrape-sku.yml
CLAUDE.md            # guidance for Claude Code sessions - keep it
README.md
```

Do **not** copy `data/` (start fresh). Skip `Dockerfile`,
`setup-gcp-wif.sh`, and `.github/workflows/deploy.yml` unless you're
doing Tier 2.

### 2. Check two settings

1. **Workflow permissions**: Settings → Actions → General → Workflow
   permissions → "Read and write permissions" must be allowed, or the
   data-commit step can't push. (The workflow also declares
   `permissions: contents: write`, which suffices on default settings.)
2. **Default branch** (only matters for scheduled runs): cron schedules
   fire only from the repo's default branch. The manual "Run workflow"
   button works from any branch.

### 3. Verify

Actions tab → "Scrape SKU counts" → **Run workflow**. The run takes a
few minutes (browser install + scrape). When green, a bot commit
`SKU counts YYYY-MM-DD` appears containing `data/sku_counts.csv` with
one row per site/category:

```
timestamp_utc,site,category,sku_count,status,note
2026-07-03T09:12:44+00:00,sportoutlet,all,8042,ok,
2026-07-03T09:12:44+00:00,sportoutlet,Klær,4275,ok,
2026-07-03T09:13:10+00:00,xxl,all,23246,ok,
...
```

Each run appends rows, so re-running over time builds a trend you can
plot straight from the repo. Sites whose scrapers aren't implemented yet
(`antonsport`, `intersport`, `sport1`) appear as `status=error` rows —
that's by design (failed reads are NULL, never guessed).

To run on a schedule, uncomment the `schedule:` block in
`scrape-sku.yml` and merge it to the default branch.

### If a scrape fails

- Read the run log: `main.py` prints one line per site/category with
  the error reason.
- All rows error → the run exits 1 and shows red; single-site failures
  stay green (other sites' data still lands).
- Site-specific behaviors and known traps (rendering caps, scrambled
  slugs, consent banners, the eSales interception) are documented in
  `CLAUDE.md` under "Hard-won gotchas" — paste the failing run's log
  into a Claude Code session on the repo and it has what it needs.

---

## Tier 2: GCP Cloud Run fallback (optional)

What you get: a **Deploy to Cloud Run** button in the Actions tab that
builds the image, deploys/updates a Cloud Run Job, executes it, and
prints its logs into the workflow run — with **no stored secrets**
(keyless Workload Identity Federation).

### 1. Copy the additional files

```
Dockerfile
setup-gcp-wif.sh
.github/workflows/deploy.yml
```

### 2. Create a GCP project and run the setup script

In [Cloud Shell](https://console.cloud.google.com/?cloudshell=true) on
your new project:

```bash
PROJECT=<your-project-id> \
REPO=<your-github-username>/<your-repo> \
REGION=<your-region> \
bash setup-gcp-wif.sh
```

The script is idempotent and does everything a fresh project needs:
enables APIs, creates the Artifact Registry repo (`scrapers`), the WIF
pool/provider trusting GitHub's OIDC issuer (restricted to repos owned
by your username), the `github-deployer` and `scraper-runner` service
accounts, the repo→deployer binding, and the deployer's roles. It
prints the exact `workload_identity_provider` string (contains your
project number) and service-account email at the end.

### 3. Point the workflow at your project

In `.github/workflows/deploy.yml`, replace the placeholders in the
`env:` block (`PROJECT_ID`, `REGION`) and in the auth step
(`workload_identity_provider`, `service_account`) with the values the
script printed. These are not secrets — hardcode them.

### 4. Verify

Actions tab → "Deploy to Cloud Run (GCP fallback)" → Run workflow with
`execute: true`. Green run = image built, job deployed, executed, and
its scraper output printed at the bottom of the run log. That log-print
loop is the whole point of the tier: edit code → dispatch → read logs →
repeat, without burning GitHub-runner minutes or fighting an IP block.

---

## Troubleshooting reference

| Symptom | Cause | Fix |
|---|---|---|
| Data-commit step fails with 403 | Workflow permissions read-only | Settings → Actions → General → "Read and write permissions" |
| Cron never fires | Workflow not on the **default** branch, or schedule still commented out | Fix default branch / uncomment `schedule:` |
| `Permission 'iam.serviceAccounts.getAccessToken' denied` on first gcloud/docker use | Repo→deployer WIF binding missing (the auth step itself still "succeeds") | Re-run `setup-gcp-wif.sh` with your `REPO` |
| `denied: ... artifactregistry.repositories.uploadArtifacts` | Same as above — token exchange failed earlier | Same fix |
| `PERMISSION_DENIED: Permission denied for all log views` | Deployer lacks `roles/logging.viewer` | Re-run the script (it grants it) |
| A category row shows `error - no count for 'X' in N API responses` (xxl) | The eSales tree labels that category differently than the nav text | `python debug_page.py <category-url>` prints every label/count pair; map the right label |
| Sport Outlet numbers suddenly tiny (~1700) | Someone reverted to DOM/scroll counting | Counts must come from `/api/v1/categories` — see CLAUDE.md gotchas |
| Playwright `Executable doesn't exist` | Browser install step skipped | `playwright install --with-deps chromium` after `pip install` |

## Costs

- Tier 1: free on a public repo; a run is ~3-5 min of the 2000 free
  private-repo Actions minutes/month, only when you trigger it.
- Tier 2: pennies — one image build per dispatch, job executions bill
  per-second, Artifact Registry storage is MBs.
