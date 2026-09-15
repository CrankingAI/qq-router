# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/CrankingAI/qq-router/security/advisories/new)
rather than opening a public issue. You should get an acknowledgement within a
few days.

## What qq does with your credentials

**Nothing leaves your machine except the prompt.** qq makes one HTTPS call, to
whichever backend you configured: the Azure AI Foundry endpoint, or OpenRouter.
There is no telemetry, no analytics, no proxy, and no third-party service in
the path. The one exception is opt-in: with `--search`, the model may call a
search tool, and qq then sends the model's search query (derived from your
prompt, though not your prompt verbatim) to the Brave Search API and hands the
results back to the model. Search is off unless you turn it on.

**Entra ID is the default and the recommendation.** With `QQ_AUTH=entra`, qq
holds no secret at all. It asks `DefaultAzureCredential` for a short-lived
bearer token scoped to `https://ai.azure.com/.default` and passes a token
provider to the SDK so the token is refreshed per request rather than cached
until it expires. Revoking access is an Azure role change, not a key rotation.

**API keys, when you use them, are stored deliberately.** `qq config set api_key`
writes to a config file in your user config directory with `0600` permissions in
a `0700` directory, written atomically via a temp file that is created with
those permissions from the start. qq never appends secrets to `.zshrc` or any
other shell rc file.

**Each backend keeps its own key.** The Azure key and the OpenRouter key are
stored under separate names and resolved separately, so switching provider never
sends one service's credential to the other. The Brave Search key, when you
configure one, is a third named secret with the same handling: it is sent only
to Brave, as a header, never in a URL. All three are written with the same
`0600` permissions and all three are masked everywhere.

**Keys are never printed.** `qq config show`, `qq config get api_key`, and
`qq doctor` all render secrets as a fixed mask that reveals neither the value
nor its length. `--verbose` diagnostics contain only the deployment name, the
routed model, latency, and token counts. Tests assert that the API key does not
appear in stdout or stderr on either the success or the failure path.

**Errors are summarised, not dumped.** Azure SDK exceptions are translated into
short messages with a remediation hint. Raw response bodies are truncated and no
request headers are echoed.

When the OpenRouter backend is selected, qq sends attribution headers naming
the tool (`HTTP-Referer`, `X-OpenRouter-Title`, `X-OpenRouter-Categories`) and
an opt-in `X-OpenRouter-Metadata` header that asks for routing details back.
None of these carry user data; they identify the tool, not the person.

## Prompt content

Whatever you type, and whatever you pipe in, is sent to your configured
backend. On OpenRouter that means an aggregator and then an upstream provider,
so consider `provider: {data_collection: "deny"}` semantics on your OpenRouter
account settings if that matters to you. `git diff | qq`
sends your diff. `cat .env | qq` would send your secrets. Piped input is
truncated to the last 100,000 characters, which limits cost, not exposure.
Think before you pipe.

Piped input is wrapped in a delimited block and labelled as input so the model
treats it as data. That is a usefulness measure, not a security boundary: a
determined prompt injection in piped content can still influence the answer.
Do not pipe untrusted content and then run the command it suggests without
reading it.

**Search results are untrusted content, and qq answers are commands.** With
`--search`, the top results for the model's query come back from the open web
and are handed to the model as tool output. The system prompt tells the model
to treat them as evidence rather than instructions, the tool only ever reads,
and the model can search at most twice per question. But that is the same
convention as the piped-input block, not a boundary: a page crafted to steer a
model can steer the answer, and the answer is often a command. Read a searched
answer before you run it. That is why search is off by default and only runs
when you ask for it.

## Infrastructure

The Bicep deploys one Azure AI Foundry account, one model-router deployment,
and one Foundry project on the account. The project exists so the router can be
called through the Responses API; it has no keys of its own and inherits the
account's role assignments. The Bicep does not output API keys, by design. The account is created with
`disableLocalAuth: false` so that key auth remains available; set
`disableLocalAuth: true` to refuse keys entirely and require Entra ID.

`*.bicepparam`, `.env`, and common deployment-output filenames are gitignored so
local values do not get committed.

## Supported versions

The latest release on `main` is the supported version.
