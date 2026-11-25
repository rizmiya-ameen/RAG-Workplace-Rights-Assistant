"""One-time Azure setup for the deployed demo (build step 21).

Run from backend/ after `az login` and `gh auth login`:

    uv run python ../deploy/azure_setup.py            # create or update everything
    uv run python ../deploy/azure_setup.py --dry-run  # print the plan, change nothing

Creates (idempotent: existing resources are kept, settings are re-applied):
- resource group rg-workplace-rights (Australia East);
- Container Apps environment + the API app wra-api: 1 vCPU / 2 GiB, scale 0..1, external
  HTTPS ingress on port 8000, public image from ghcr.io (no registry password), secrets from
  backend/.env (DATABASE_URL, LLM_API_KEY, LANGFUSE_*) as Container Apps secrets;
- Static Web App wra-web (Free) for the UI; CORS_ORIGINS on the API = its URL;
- an Entra app + federated credential so GitHub Actions on main can deploy with OIDC
  (Contributor on the resource group only);
- GitHub repo variables (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID, API_URL)
  and the secret AZURE_STATIC_WEB_APPS_API_TOKEN.

Secret values go from backend/.env straight into `az`/`gh` arguments or stdin; they are
never printed (command output is captured, and any secret in an error message is masked).
max-replicas is 1: the rate limiter keeps its counts in memory (app/ratelimit.py).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO = "rizmiya-ameen/RAG-Workplace-Rights-Assistant"
IMAGE_REPO = "rizmiya-ameen/workplace-rights-api"
IMAGE = f"ghcr.io/{IMAGE_REPO}:latest"
LOCATION = "australiaeast"
SWA_LOCATION = "eastasia"  # Static Web Apps' nearest region; content is served from a CDN
RG = "rg-workplace-rights"
ENV = "cae-workplace-rights"
API = "wra-api"
WEB = "wra-web"
OIDC_APP = "gh-workplace-rights-deploy"

BACKEND = Path(__file__).resolve().parents[1] / "backend"
SECRET_KEYS = {  # Container Apps secret name -> backend/.env key -> app env var
    "database-url": "DATABASE_URL",
    "llm-api-key": "LLM_API_KEY",
    "langfuse-public-key": "LANGFUSE_PUBLIC_KEY",
    "langfuse-secret-key": "LANGFUSE_SECRET_KEY",
}
PLAIN_KEYS = ["LLM_BASE_URL", "LLM_MODEL", "LLM_REASONING_EFFORT", "LANGFUSE_HOST"]

DRY_RUN = False
_SECRETS: list[str] = []


def _tool(name: str, fallback: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if Path(fallback).exists():
        return fallback
    sys.exit(f"{name} not found: install it first (see deploy/README.md)")


def _az_command() -> list[str]:
    """On Windows `az` is a .cmd file, so cmd.exe would parse the arguments: the "&" in a Neon
    connection string would split the command. Run the CLI's bundled Python directly."""
    if sys.platform != "win32":
        return [_tool("az", "/usr/bin/az")]
    cli = Path(_tool("az", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"))
    python = cli.parent.parent / "python.exe"
    if not python.exists():
        sys.exit(f"Azure CLI's python.exe not found next to {cli}")
    return [str(python), "-IBm", "azure.cli"]


AZ: list[str] = []
GH = ""


def _mask(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


def run(tool: str, args: list[str], *, stdin: str | None = None, check: bool = True) -> str:
    """Run az or gh. In --dry-run only read-only calls (show/list) really run."""
    shown = _mask(" ".join(args))[:160]
    if DRY_RUN and not {"show", "list"} & set(args):
        print(f"  [dry-run] {tool} {shown}")
        return ""
    command = (AZ if tool == "az" else [GH]) + args
    out = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if DRY_RUN and check and out.returncode != 0:
        return "<created-by-the-real-run>"  # reading something the dry run didn't create
    if check and out.returncode != 0:
        sys.exit(f"failed: {tool} {shown}\n{_mask(out.stderr.strip())[-800:]}")
    return out.stdout.strip() if out.returncode == 0 else ""


def az(*args: str, check: bool = True) -> str:
    return run("az", [*args, "--only-show-errors"], check=check)


def exists(*args: str) -> bool:
    return bool(az(*args, "--query", "id", "-o", "tsv", check=False))


def image_is_public() -> bool:
    """Anonymous pull check: ghcr.io hands anyone a pull token, but the manifest only
    answers 200 for a public image."""
    scope = f"repository:{IMAGE_REPO}:pull"
    r = httpx.get("https://ghcr.io/token", params={"scope": scope}, timeout=20)
    token = r.json().get("token") if r.status_code == 200 else None
    if not token:
        return False
    r = httpx.head(
        f"https://ghcr.io/v2/{IMAGE_REPO}/manifests/latest",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.v2+json",
        },
        timeout=20,
    )
    return r.status_code == 200


def step(text: str) -> None:
    print(f"\n== {text}")


def main() -> None:
    global AZ, GH, DRY_RUN
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    DRY_RUN = ap.parse_args().dry_run

    AZ = _az_command()
    GH = _tool("gh", r"C:\Program Files\GitHub CLI\gh.exe")

    env = dotenv_values(BACKEND / ".env")
    missing = [k for k in ("DATABASE_URL", "LLM_API_KEY") if not env.get(k)]
    if missing:
        sys.exit(f"backend/.env is missing: {', '.join(missing)} (see deploy/README.md)")
    _SECRETS.extend(v for k, v in env.items() if v and ("KEY" in k or "TOKEN" in k or "URL" in k))

    step(f"Image ghcr.io/{IMAGE_REPO}")
    if not image_is_public():
        sys.exit(
            "The image is not publicly pullable yet. Merge to main once so the Deploy workflow\n"
            "pushes it, then on GitHub: your profile -> Packages -> workplace-rights-api ->\n"
            "Package settings -> Change visibility -> Public (see deploy/README.md)."
        )
    print("  public: Azure pulls it without a password")

    step("Azure account")
    account = json.loads(az("account", "show", "-o", "json"))
    sub, tenant = account["id"], account["tenantId"]
    print(f"  subscription: {account['name']}")
    az("config", "set", "extension.use_dynamic_install=yes_without_prompt")
    for ns in ("Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.Web"):
        az("provider", "register", "--namespace", ns, "--wait")

    step(f"Resource group {RG} ({LOCATION})")
    az("group", "create", "-n", RG, "-l", LOCATION, "-o", "none")

    step(f"Container Apps environment {ENV}")
    if not exists("containerapp", "env", "show", "-n", ENV, "-g", RG):
        az("containerapp", "env", "create", "-n", ENV, "-g", RG, "-l", LOCATION, "-o", "none")

    step(f"API {API} (scale 0..1, 1 vCPU / 2 GiB)")
    secrets = [f"{name}={env[key]}" for name, key in SECRET_KEYS.items() if env.get(key)]
    env_vars = [f"{key}=secretref:{name}" for name, key in SECRET_KEYS.items() if env.get(key)]
    env_vars += [f"{k}={env[k]}" for k in PLAIN_KEYS if env.get(k)]
    if not exists("containerapp", "show", "-n", API, "-g", RG):
        az(
            "containerapp", "create", "-n", API, "-g", RG, "--environment", ENV,
            "--image", IMAGE,
            "--target-port", "8000", "--ingress", "external",
            "--cpu", "1", "--memory", "2Gi", "--min-replicas", "0", "--max-replicas", "1",
            "--secrets", *secrets, "--env-vars", *env_vars, "-o", "none",
        )  # fmt: skip
    else:
        az("containerapp", "secret", "set", "-n", API, "-g", RG, "--secrets", *secrets,
           "-o", "none")  # fmt: skip
        az(
            "containerapp", "update", "-n", API, "-g", RG, "--min-replicas", "0",
            "--max-replicas", "1", "--set-env-vars", *env_vars, "-o", "none",
        )  # fmt: skip
    fqdn = az("containerapp", "show", "-n", API, "-g", RG, "--query",
              "properties.configuration.ingress.fqdn", "-o", "tsv")  # fmt: skip
    api_url = f"https://{fqdn}"
    print(f"  API: {api_url}")

    step(f"Static Web App {WEB} (Free)")
    if not exists("staticwebapp", "show", "-n", WEB, "-g", RG):
        az("staticwebapp", "create", "-n", WEB, "-g", RG, "-l", SWA_LOCATION, "--sku", "Free",
           "-o", "none")  # fmt: skip
    host = az("staticwebapp", "show", "-n", WEB, "-g", RG, "--query", "defaultHostname",
              "-o", "tsv")  # fmt: skip
    web_url = f"https://{host}"
    print(f"  UI: {web_url}")
    token = az("staticwebapp", "secrets", "list", "-n", WEB, "-g", RG, "--query",
               "properties.apiKey", "-o", "tsv")  # fmt: skip
    _SECRETS.append(token)

    step("CORS: the API accepts browser calls from the UI only")
    az("containerapp", "update", "-n", API, "-g", RG, "--set-env-vars",
       f"CORS_ORIGINS={web_url}", "-o", "none")  # fmt: skip

    step(f"GitHub OIDC deploy identity {OIDC_APP}")
    app_id = az("ad", "app", "list", "--display-name", OIDC_APP, "--query", "[0].appId",
                "-o", "tsv")  # fmt: skip
    if not app_id:
        app_id = az("ad", "app", "create", "--display-name", OIDC_APP, "--query", "appId",
                    "-o", "tsv")  # fmt: skip
    if not az("ad", "sp", "show", "--id", app_id, "--query", "id", "-o", "tsv", check=False):
        az("ad", "sp", "create", "--id", app_id, "-o", "none")
    creds = az("ad", "app", "federated-credential", "list", "--id", app_id, "--query",
               "[].name", "-o", "tsv")  # fmt: skip
    # GitHub's OIDC subject is either "repo:owner/name:..." or, for newer repos,
    # "repo:owner@<owner id>/name@<repo id>:..." (immutable ids). Trust both, main only.
    owner_id, repo_id = run(
        "gh", ["api", f"repos/{REPO}", "--jq", "[.owner.id, .id] | @tsv"]
    ).split()
    owner, name = REPO.split("/")
    subjects = {
        "github-main": f"repo:{REPO}:ref:refs/heads/main",
        "github-main-ids": f"repo:{owner}@{owner_id}/{name}@{repo_id}:ref:refs/heads/main",
    }
    for cred_name, subject in subjects.items():
        if cred_name in creds.split():
            continue
        cred = {
            "name": cred_name,
            "issuer": "https://token.actions.githubusercontent.com",
            "subject": subject,
            "audiences": ["api://AzureADTokenExchange"],
        }
        az("ad", "app", "federated-credential", "create", "--id", app_id, "--parameters",
           json.dumps(cred), "-o", "none")  # fmt: skip
    scope = f"/subscriptions/{sub}/resourceGroups/{RG}"
    # check=False: the assignment may already exist, which is fine
    az("role", "assignment", "create", "--assignee", app_id, "--role", "Contributor",
       "--scope", scope, "-o", "none", check=False)  # fmt: skip

    step("GitHub variables and secret")
    for name, value in {
        "AZURE_CLIENT_ID": app_id,
        "AZURE_TENANT_ID": tenant,
        "AZURE_SUBSCRIPTION_ID": sub,
        "API_URL": api_url,
    }.items():
        run("gh", ["variable", "set", name, "--repo", REPO, "--body", value])
    run("gh", ["secret", "set", "AZURE_STATIC_WEB_APPS_API_TOKEN", "--repo", REPO], stdin=token)

    print(f"\nDone. API {api_url}  UI {web_url}")
    print("Next: run the Deploy workflow (gh workflow run deploy.yml) or push to main.")


if __name__ == "__main__":
    main()
