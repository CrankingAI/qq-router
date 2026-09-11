# qq

[![CI](https://github.com/CrankingAI/qq-router/actions/workflows/ci.yml/badge.svg)](https://github.com/CrankingAI/qq-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Azure AI Foundry](https://img.shields.io/badge/Azure-AI%20Foundry-0078D4?logo=microsoftazure&logoColor=white)](https://learn.microsoft.com/azure/foundry/)
[![Model Router](https://img.shields.io/badge/model--router-2025--11--18-5E5E5E)](https://learn.microsoft.com/azure/foundry/openai/concepts/model-router)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Ask a quick question from your terminal. Get a short answer back.**

`qq` is for the questions that do not deserve a browser tab. A model router
picks a cheap model for easy questions and a stronger one for hard ones, so you
do not have to think about which model to use.

Two backends, same idea: **Azure AI Foundry Model Router** (the default) and
**OpenRouter**.

```console
$ qq how do I list all my github repos
```
```bash
gh repo list --limit 1000
```
```console
$ qq --verbose what is a CNAME
A CNAME record aliases one DNS name to another...
[deployment=qq-router model=gpt-5.6-luna-2026-07-09 latency=1.97s tokens=196in/83out]
```

Answers go to stdout, diagnostics to stderr, so `qq` composes in a pipeline.

## Architecture

Deliberately tiny. There is no API, no gateway, no database, no Key Vault, and
no proxy. The CLI calls Azure directly.

```mermaid
flowchart TD
    T["terminal"] --> Q["qq CLI<br/>(local Python tool)"]
    Q -->|"HTTPS + Entra ID or API key"| F["Azure AI Foundry<br/>/openai/v1"]
    Q -.->|"HTTPS + API key"| O["OpenRouter<br/>/api/v1"]

    F --> R["model-router deployment<br/>mode: balanced"]
    R -->|simple question| L["gpt-5.6-luna"]
    R -->|moderate| TE["gpt-5.6-terra"]
    R -->|hard question| S["gpt-5.6-sol"]

    O --> A["openrouter/auto<br/>cost_tier"]
    A --> M["whichever model wins<br/>for the task type"]

    subgraph Azure["Azure subscription (provisioned by Bicep)"]
        F
        R
        L
        TE
        S
    end
    subgraph OR["OpenRouter account (nothing to provision)"]
        O
        A
        M
    end
```

On Azure the whole server side is one `Microsoft.CognitiveServices/accounts`
resource of kind `AIServices` plus one `model-router` deployment on it. On
OpenRouter there is nothing to provision at all: an API key is the entire setup.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| Azure subscription | hosts the Foundry resource | — |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) 2.60+ | deploy and sign in | `brew install azure-cli` |
| [`jq`](https://jqlang.github.io/jq/) | the scripts parse JSON | `brew install jq` |
| [`uv`](https://docs.astral.sh/uv/) or [`pipx`](https://pipx.pypa.io/) | installs `qq` in its own environment | `brew install uv` |
| Python 3.11+ | provided by uv/pipx if you lack it | `brew install python` |

You need permission to create a resource group and a Cognitive Services account
in the target subscription. Granting yourself the inference role additionally
needs `Owner` or `User Access Administrator`; without it, pass `--no-grant` and
have someone assign the role for you.

## 1. Deploy Azure

```bash
git clone https://github.com/CrankingAI/qq-router.git
cd qq-router
az login
./scripts/deploy.sh
```

That provisions everything at subscription scope, creating the resource group as
it goes. Useful options:

```bash
./scripts/deploy.sh --dry-run                       # what-if, changes nothing
./scripts/deploy.sh --env prod --location swedencentral
./scripts/deploy.sh --routing-mode cost --capacity 30
./scripts/deploy.sh --subscription <id>             # default: current az subscription
./scripts/deploy.sh --help
```

Before deploying, the script checks that `model-router` and the three OpenAI
models exist in your chosen region and stops with a clear message if they do
not. Afterwards it verifies the router is pinned to OpenAI models only.

The deployment is idempotent: re-running converges the same resources.

### What gets created

| Resource | Purpose |
|---|---|
| `rg-qq-dev` | resource group |
| `qq-dev-<hash>` | Foundry account, `kind: AIServices`, SKU `S0` |
| `qq-router` | `model-router` deployment, `GlobalStandard`, capacity 10 |

### Parameters

Edit `infra/main.bicepparam.example`, copy it to `infra/main.bicepparam` (which
is gitignored), or pass `--parameters` yourself.

| Parameter | Default | Notes |
|---|---|---|
| `namePrefix` | `qq` | resource name prefix |
| `environment` | `dev` | name suffix and tag |
| `location` | `eastus2` | must support model-router |
| `routerDeploymentName` | `qq-router` | what `qq` sends as the model id |
| `routingMode` | `balanced` | `balanced`, `cost`, or `quality` |
| `routerModelVersion` | `2025-11-18` | Microsoft updates this version in place |
| `routerCapacity` | `10` | thousands of tokens per minute |
| `routerModels` | three `gpt-5.6-*` models | the routing subset |
| `principalId` | `''` | grants Foundry User; empty skips it |
| `logAnalyticsWorkspaceId` | `''` | enables diagnostics; empty skips it |
| `disableLocalAuth` | `false` | `true` refuses API keys entirely |

`routerModels` is parameterised so you can change the lineup without touching
the template. Supply at least two models: a single-model subset disables
automatic failover.

## 2. Install the CLI

```bash
./scripts/setup-cli.sh
```

This picks `uv` if you have it and `pipx` otherwise, installs `qq` into its own
isolated environment, discovers your endpoint from Azure, writes the config
file, and runs `qq doctor`. It does not touch your shell rc files unless you
pass `--update-shell`.

```bash
./scripts/setup-cli.sh --resource-group rg-qq-dev
./scripts/setup-cli.sh --use-api-key        # store a key instead of using Entra
./scripts/setup-cli.sh --tool pipx
./scripts/setup-cli.sh --help
```

Installing by hand instead:

```bash
uv tool install git+https://github.com/CrankingAI/qq-router
qq config set endpoint https://<your-resource>.openai.azure.com
qq config set deployment qq-router
qq config set tenant "$(az account show --query tenantId -o tsv)"
```

If `qq` is not found afterwards, `~/.local/bin` is not on your `PATH`. Add it,
or re-run with `--update-shell`.

## Authentication

`qq` supports both. Entra ID is the default and the better choice for anything
shared, because there is no secret to leak or rotate.

### Microsoft Entra ID (passwordless, recommended)

```bash
az login
qq config set auth entra
qq config set tenant "$(az account show --query tenantId -o tsv)"
```

`qq` asks `DefaultAzureCredential` for a token scoped to
`https://ai.azure.com/.default` and hands the SDK a token *provider*, so the
token refreshes per request rather than expiring mid-session. Tokens are cached
on disk with `0600` permissions until shortly before expiry, which saves about
0.65s per question; disable with `QQ_NO_TOKEN_CACHE=1`.

You need the **Foundry User** role on the Foundry account. `deploy.sh` grants it
to you automatically. To grant someone else:

```bash
az role assignment create \
  --role 53ca6127-db72-4b80-b1b0-d745d6d5456d \
  --assignee <object-id> \
  --scope "/subscriptions/<sub>/resourceGroups/rg-qq-dev/providers/Microsoft.CognitiveServices/accounts/<account>"
```

> **Setting `tenant` matters.** `DefaultAzureCredential` asks the Azure CLI for a
> token using the CLI's *current* subscription, which is global state any shell
> can change. If it points at another tenant you get
> `Tenant provided in token does not match resource token` despite being logged
> in. Pinning the tenant makes `qq` immune to that.

### API key

```bash
qq config set api_key '<key>'     # stored 0600, never printed
qq config set auth key
```

Or per-invocation, without persisting anything:

```bash
QQ_API_KEY=... qq what is a CNAME
```

Get a key with:

```bash
az cognitiveservices account keys list -g rg-qq-dev -n <account> --query key1 -o tsv
```

## Usage

```bash
qq how do I list all my github repos          # quoting optional
qq "explain EIP-3009 in two sentences"        # quoting fine too
qq -v what is a CNAME                         # model and latency on stderr
qq -vv what is a CNAME                        # plus connection and request context
qq -vvv what is a CNAME                       # plus server-side timing breakdown
qq --model gpt-5.6-sol explain TCP slow start # bypass the router
qq --version
qq --help
```

### Piping and stdin

stdin alone becomes the question:

```bash
echo "what is EIP-3009?" | qq
```

stdin plus a question sends the question first and the piped text as labelled
input:

```bash
git diff | qq summarize this
cat error.txt | qq explain this error
kubectl get pods | qq which of these look unhealthy
qq how do I list all my repos | pbcopy
```

Piped input is truncated to the last 100,000 characters, keeping the tail
because that is where errors are.

### Subcommands

```bash
qq doctor          # diagnose install, config, auth, and a live call
qq config show     # resolved settings and where each came from
qq config set KEY VALUE
qq config unset KEY
qq config path
```

`doctor` and `config` are only treated as subcommands when the rest of the line
looks like one. `qq doctor who wrote this` is a question. Force it with
`qq --ask doctor`.

## Configuration

Precedence, highest first: flags, `QQ_*` environment variables,
`AZURE_OPENAI_*` environment variables, the config file, built-in defaults.

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `QQ_ENDPOINT` | `endpoint` | — | Foundry endpoint; `/openai/v1` is appended for you |
| `QQ_DEPLOYMENT` | `deployment` | `qq-router` | router deployment name |
| `QQ_API_KEY` | `api_key` | — | API key; leave unset to use Entra ID |
| `QQ_AUTH` | `auth` | `auto` | `auto`, `entra`, or `key` |
| `QQ_TENANT_ID` | `tenant` | — | Entra tenant owning the resource |
| `QQ_MODEL` | `model` | — | address a deployment directly |
| `QQ_API` | `api` | `auto` | `auto`, `chat`, or `responses` |
| `QQ_ROUTER` | `router` | — | what the Azure deployment is backed by, shown at `-vv` |
| `QQ_PROVIDER` | `provider` | `azure` | `azure` or `openrouter` |
| `QQ_OPENROUTER_API_KEY` | `openrouter_api_key` | — | OpenRouter key; `OPENROUTER_API_KEY` also works |
| — | `openrouter_model` | `openrouter/auto` | OpenRouter model slug |
| `QQ_COST_TIER` | `cost_tier` | — | `low`, `medium`, `high`, `xhigh`, `max` |
| `QQ_ALLOWED_MODELS` | `allowed_models` | — | comma-separated patterns the auto-router may pick from |
| `QQ_TIMEOUT` | `timeout` | `60` | request timeout in seconds |
| `QQ_CONFIG_DIR` | — | OS default | override the config directory |
| `QQ_NO_STREAM` | — | — | disable streaming |
| `QQ_NO_TOKEN_CACHE` | — | — | disable the Entra token cache |
| `QQ_OTEL` | — | — | `1` enables OpenTelemetry tracing |

`AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` are honoured as fallbacks so
an existing Azure setup works with no extra configuration.

The config file lives at `~/.config/qq/config.toml` on macOS and Linux
(`%APPDATA%\qq\config.toml` on Windows), mode `0600` in a `0700` directory.

Each provider keeps its own persisted keys, so both can be configured at once
and `QQ_PROVIDER=openrouter qq ...` never reaches for an Azure endpoint or an
Azure deployment name.

### Which API surface

`qq` defaults to Chat Completions. That is not nostalgia: as of September 2026
the `model-router` model advertises only the `chatCompletion` capability in
every region, and calling `/openai/v1/responses` against a router deployment
returns `400 The requested operation is unsupported`. Direct model deployments
such as `gpt-5.6-luna` do support Responses, so `--api responses` is available
for those. Check for yourself:

```bash
az cognitiveservices model list --location eastus2 \
  --query "[?model.name=='model-router'].model.capabilities" -o json
```

Both surfaces report the model that actually served the request, which is what
`--verbose` prints. `qq` never guesses the routed model.

## Verbose output

`-v` is countable. Each level adds lines without moving the ones below it, so
the line you already know never shifts.

```console
$ qq -v what is a CNAME
A CNAME record aliases one DNS name to another...
[deployment=qq-router model=gpt-5.6-luna-2026-07-09 latency=1.89s tokens=196in/33out]
```

`deployment` is what `qq` addressed, `model` is what the router actually chose.

```console
$ qq -vv gh command to clone repo
gh repo clone OWNER/REPO
[deployment=qq-router model=gpt-5.6-luna-2026-07-09 latency=2.18s tokens=196in/30out]
[router=model-router:2025-11-18 host=qq-dev-abc.openai.azure.com api=chat auth=entra stream=off request=chatcmpl-EMuZANnJ0Rt]
```

`-vv` answers "what am I actually talking to". `router` is the model backing
your deployment, which is how you confirm you are on Microsoft's Model Router
rather than a plain model deployment.

```console
$ qq -vvv what is a CNAME
A CNAME record aliases one DNS name to another...
[deployment=qq-router model=gpt-5.6-luna-2026-07-09 latency=1.89s tokens=196in/33out]
[router=model-router:2025-11-18 host=qq-dev-abc.openai.azure.com api=chat auth=entra stream=off request=chatcmpl-EMuZDBiHxAr]
[server pre_inference=43ms engine_ttft=88ms engine_ttlt=315ms engine_tbt=7ms service_ttft=358ms service_ttlt=557ms visible_ttft=315ms]
[detail replica=gpt56-l-usc-gb3-oai-oe-5b5xdp cached=0 reasoning=0 token_cache=hit tenant=00000000-1111-2222-3333-444444444444 overhead=1.34s]
```

`-vvv` is for "why did that feel slow". `overhead` is wall time the service did
not account for, so a large value points at the local side, usually token
acquisition or TLS setup, rather than at the model.

| Level | Shows |
|---|---|
| `-v` | routed model, latency, token counts |
| `-vv` | router identity, host, API surface, auth mode, streaming, request id |
| `-vvv` | server-side timing breakdown, serving replica, cached and reasoning tokens, token cache state, tenant, client overhead |

Every level goes to stderr, so `qq` still composes:

```bash
qq -vvv ... 2>/dev/null   # answer only
qq -vvv ... >/dev/null    # diagnostics only
```

Two caveats. `router` is recorded when `setup-cli.sh` runs, because the
inference API does not report what a deployment is backed by; it reflects setup
time, and shows `router=?` if never recorded. The `server` and `replica` fields
come from `routing` and `latency_checkpoint`, which are Azure extensions rather
than part of the OpenAI schema, so those lines are omitted entirely if a future
service update stops returning them.

## OpenRouter

[OpenRouter](https://openrouter.ai) is an aggregator with its own auto-router,
which makes it a close analogue of Azure's Model Router. It needs no
infrastructure: a key is the whole setup.

```bash
qq config set provider openrouter
qq config set openrouter_api_key '<key>'    # from https://openrouter.ai/keys
qq doctor
qq what is the gh cli command to list all repos
```

Or per-invocation, leaving your Azure setup as the default:

```bash
QQ_PROVIDER=openrouter qq explain EIP-3009
qq --provider openrouter --cost-tier low what is a CNAME
```

### How the two map

| Azure AI Foundry | OpenRouter |
|---|---|
| `model-router` deployment | the `openrouter/auto` model |
| `routingMode` (`balanced`/`cost`/`quality`) | `cost_tier` (`low`…`max`) |
| `routerModels` subset in Bicep | `allowed_models` patterns |
| `response.model` | `response.model` |
| Entra ID or API key | API key only |

They choose differently. Azure judges how hard the question is. OpenRouter
classifies the prompt into one of roughly thirty task types, then ranks
candidates by what the OpenRouter community actually spent on that task type
over the previous week, filtered by your cost tier.

`cost_tier` is a percentile *band*, not a ceiling, so `low` also excludes models
cheaper than the band. Setting nothing routes roughly as if you asked for `low`.

### Restricting what it may pick

```bash
qq config set cost_tier low
qq config set allowed_models 'openai/*,anthropic/*,google/gemini-2.5-flash*'
```

Patterns accept wildcards. This is the equivalent of pinning `routerModels` in
the Bicep, and it is the knob to reach for if you want to keep spend predictable
or stay with one vendor.

### Seeing what it did

```console
$ qq --provider openrouter -vvv what is a CNAME
A CNAME record aliases one DNS name to another...
[deployment=openrouter/auto model=anthropic/claude-sonnet-4.5 latency=1.84s tokens=15in/150out]
[provider=openrouter router=openrouter/auto:low host=openrouter.ai api=chat auth=key stream=off cost=$0.000123 request=gen-abc123]
[detail upstream=Anthropic strategy=auto task=qa_knowledge token_cache=n/a overhead=0.31s]
```

OpenRouter reports the real charge for each request, so `cost` at `-vv` is the
actual money spent, not an estimate. `upstream` and `task` come from an opt-in
metadata header that `qq` sends for you.

### Billing

**OpenRouter is billed by OpenRouter, not against Azure credits.** That is the
main reason it is not the default. A negative balance returns `402` even on
free models. Free model variants carry a `:free` suffix and are capped at 50
requests a day until you have bought at least 10 dollars of credit, then 1000.

## Cost

The architecture has **no fixed cost**. Neither the `S0` Cognitive Services
account nor a `GlobalStandard` model deployment carries an hourly or
provisioning charge, so an idle `qq` costs nothing. Only fine-tuned model
deployments bill for hosting, and `qq` does not create one.

You pay per token: the model-router input meter plus the normal input/output
tokens of whichever model it selects. Typical terminal questions are a few
hundred tokens, so per-question cost is fractions of a cent. Piping a large diff
costs more; that is what the 100,000 character cap is for.

`routingMode` is the cost lever. `cost` biases toward cheaper models, `quality`
toward stronger ones, `balanced` sits between.

How much routing you actually observe depends on how far apart your subset is.
The default three `gpt-5.6-*` models are close in capability, and in testing
`balanced` selected `gpt-5.6-luna` for everything from `what does chmod 755
mean` to a proof of the halting problem. Add a genuinely cheaper model such as
`gpt-5.4-nano` to `routerModels` if you want the router to have somewhere
cheaper to go. `--verbose` always shows which model answered.

> **Third-party models are excluded on purpose.** The router's full candidate
> pool includes Anthropic, xAI, DeepSeek and Meta models, which bill separately
> rather than against Azure consumption. `infra/main.bicep` pins the subset to
> three OpenAI models, and `deploy.sh` fails the deployment if any non-OpenAI
> model or an empty subset is detected. Leaving `routerModels` empty would let
> the router reach third-party models, so do not do that casually.

Current rates: [Azure AI Foundry Models pricing](https://azure.microsoft.com/pricing/details/ai-foundry-models/).

## Observability

Client-side tracing is opt-in, since `qq` makes one call and should start fast:

```bash
uv tool install --with 'qq-cli[otel]' git+https://github.com/CrankingAI/qq-router
QQ_OTEL=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 qq what is a CNAME
```

Server-side needs no code. Point the Bicep at a Log Analytics workspace and
Azure records per-request latency, token counts, and the selected model:

```bash
./scripts/deploy.sh   # after setting logAnalyticsWorkspaceId in your bicepparam
```

It is off by default because a workspace is a billable resource.

## Fork and run your own

Nothing in this repo is specific to one Azure account.

1. Fork, then `git clone` your fork.
2. `az login` and select your subscription.
3. `./scripts/deploy.sh --name-prefix <yours>` — names include a hash of the
   resource group id, so they will not collide with anyone else's.
4. `./scripts/setup-cli.sh`
5. `qq doctor`

To change the model lineup, edit `routerModels` in
`infra/main.bicepparam.example` and redeploy. Routing changes take up to five
minutes to take effect.

## Uninstall

```bash
uv tool uninstall qq-cli          # or: pipx uninstall qq-cli
rm -rf ~/.config/qq               # config file and token cache
./scripts/teardown.sh --env dev   # delete the Azure resources
```

`teardown.sh` asks you to type the resource group name unless you pass `--yes`.
Add `--purge` to purge the soft-deleted Foundry account so the name is
immediately reusable.

## Security

Short version: the prompt is the only thing that leaves your machine, keys are
never printed, and Entra ID is the default. See [SECURITY.md](SECURITY.md).

One thing worth repeating: `qq` sends whatever you pipe into it. `git diff | qq`
sends your diff. Think before you pipe.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format .
az bicep build --file infra/main.bicep --stdout > /dev/null
shellcheck scripts/*.sh
```

CI runs the tests on Python 3.11 through 3.13, lints and format-checks with
Ruff, builds and lints the Bicep, ShellChecks the scripts, and scans for
secrets. **No Azure credentials are used in CI** — the Azure client is stubbed,
so a fork runs the full suite unchanged.

## License

[MIT](LICENSE)
