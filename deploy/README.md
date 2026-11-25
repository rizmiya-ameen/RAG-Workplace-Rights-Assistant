# Deploying to Azure

```
GitHub (main) ──CI green──► Deploy workflow
                              ├─ API image ──► ghcr.io ──► Azure Container Apps  wra-api
                              │                            (1 vCPU / 2 GiB, scale 0..1, Australia East)
                              └─ web UI (Vite build, VITE_API_BASE=API URL) ──► Azure Static Web Apps  wra-web
Container Apps ──► Neon Postgres (Sydney) · Gemini API · Langfuse
```

- **API:** Azure Container Apps, consumption plan, scale to zero. The monthly free grant
  (180,000 vCPU-seconds and 360,000 GiB-seconds) covers about 50 hours of activity at
  1 vCPU / 2 GiB; a replica stays up about 5 minutes after the last request. The first
  question after an idle spell waits for the container to start and load its three models
  (about 30-60 s); the UI says the assistant is waking up.
- **Image:** built in GitHub Actions and pushed to `ghcr.io/rizmiya-ameen/workplace-rights-api`
  (public, like the repo, so Azure pulls it without a password; it holds no secrets, they
  are injected at runtime). The models are baked in, so a new container never downloads
  from Hugging Face (`HF_HUB_OFFLINE=1`).
- **UI:** Azure Static Web Apps, Free plan. It calls the API cross-origin; the API's
  `CORS_ORIGINS` allows only the UI's URL. Security headers and a Content-Security-Policy
  are in `frontend/public/staticwebapp.config.json`.
- **Secrets:** `DATABASE_URL`, `LLM_API_KEY`, `LANGFUSE_*` live as Container Apps secrets,
  copied from `backend/.env` by the setup script. GitHub stores no Azure password: the
  Deploy workflow logs in with OpenID Connect (a federated credential trusted for this
  repo's `main` branch only, Contributor on the resource group only).
- **Database:** the same Neon project as development.

## One-time setup

1. Install the Azure CLI and log in (a browser window opens):
   ```
   winget install -e --id Microsoft.AzureCLI
   az login
   ```
2. Merge to `main` once so the Deploy workflow pushes the first image (its Azure steps
   are skipped until step 4 has run).
3. Make the image public: GitHub → your profile → **Packages** → `workplace-rights-api` →
   **Package settings** → Change visibility → **Public**. (New packages start private.)
4. Create the Azure resources, from `backend/`:
   ```
   uv run python ../deploy/azure_setup.py --dry-run   # see the plan
   uv run python ../deploy/azure_setup.py
   ```
   It prints the API and UI URLs and sets the GitHub variables `AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `API_URL` and the secret
   `AZURE_STATIC_WEB_APPS_API_TOKEN`. It stops first if the image can't be pulled
   anonymously (step 3). Safe to re-run (e.g. after rotating a key in
   `backend/.env`).
5. Deploy: `gh workflow run deploy.yml` (or any push to `main` after CI passes).

## Every deploy after that

Push to `main` → CI → Deploy: new image tagged with the commit SHA, `az containerapp update`,
`/health` smoke test, then the UI build and upload.

## Costs and limits

- Within the free grants for a demo. Set a budget alert in the Azure portal (Cost
  Management → Budgets → A$1/month, emails at 50% and 100% actual and 100% forecast).
  A budget only alerts, it never stops anything; scale 0..1 and the rate limits are what
  keep the bill at zero.
- `max-replicas` is 1 because the rate limiter (10/min and 100/day per client, 400/day in
  total) keeps its counts in memory; more replicas would need a shared store.
- The Log Analytics workspace created with the Container Apps environment keeps logs
  30 days; logs hold no questions, answers or IPs (`app/logs.py`).

## Useful commands

```
az containerapp logs show -n wra-api -g rg-workplace-rights --follow   # live JSON logs
az containerapp revision list -n wra-api -g rg-workplace-rights -o table
az containerapp update -n wra-api -g rg-workplace-rights --image ghcr.io/rizmiya-ameen/workplace-rights-api:<sha>   # roll back
az group delete -n rg-workplace-rights                                 # remove everything
```
