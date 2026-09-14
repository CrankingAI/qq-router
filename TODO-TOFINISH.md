# TODO / to finish

State as of 2026-09-14. Everything below is either a decision the maintainer
still has to make, or a known gap. Nothing here blocks daily use: both backends
work end to end, 151 tests pass, CI is green.

## Decisions to make

These are choices, not bugs. Each has a recommendation, but the call is yours.

### 1. Web search

The biggest open question. Neither backend searches today; `qq` sends one plain
chat request with no tools.

Four ways to add it, in rough order of how little they change `qq`:

| Approach | Loop in qq | New state | Keeps the router |
|---|---|---|---|
| `qq` calls a search API itself and prepends results | No | None | Yes, both |
| Provider built-in tool (Azure `web_search`, OpenRouter `:online`) | No | None | OpenRouter yes, **Azure no** |
| Foundry hosted agent with Bing grounding | Yes, poll runs | Agents, threads | Yes |
| Client-side function calling | Yes, full harness | Message history | Yes |

The Azure trap: `model-router` rejects the Responses API outright (verified,
HTTP 400), and `web_search` only exists on the Responses API. So on Azure you
either give up the router for a direct deployment, or take on an agent harness
plus provisioned resources. OpenRouter has no such constraint:
`openrouter/auto:online` is a seven-character change at $0.007 per request.

Recommendation: row one or row two, as an explicit `--search` flag, never on by
default. Never row three or four for a tool whose pitch is disposable questions.

### 2. Inject today's date into the system prompt

Not implemented. About ten tokens. Removes a class of confidently wrong "as of"
answers; the Azure model guessed the date correctly once, unprompted, which is
luck rather than design. Recommended regardless of the search decision.

### 3. Always-on verbosity

`-v` must be typed every time. A `verbose` config key and `QQ_VERBOSE` would let
the model line be permanently on. Recommended earlier, not yet requested.

### 4. Restrict OpenRouter's candidate models

`openrouter/auto` has been choosing `deepseek/deepseek-v4-flash-0731` for every
question so far. The Azure side is pinned to OpenAI only; OpenRouter is not.
`qq config set allowed_models 'openai/*'` would match, at the cost of OpenRouter
having less to choose from. Depends on whether the third-party-model stance for
Azure was about billing (does not apply, OpenRouter bills separately anyway) or
about vendor preference (does apply).

### 5. A cheaper model in the Azure routing subset

The three `gpt-5.6-*` models are close in capability. Adding `gpt-5.4-nano` to
`routerModels` gives the router somewhere genuinely cheaper to send trivial
questions. Zero fixed cost either way.

### 6. Distribution

Installed from git today. The distribution name is `qq-cli` and is PyPI-safe.
Publishing to PyPI would turn the install command into `uv tool install qq-cli`.

### 7. Naming

The repo is `qq-router`, the command is `qq`, the package is `qq-cli`. Fine, but
three names for one thing. Renaming the repo to `qq` is a one-line `gh` command
if wanted.

### 8. `EXAMPLE.md`

Sits untracked at the repo root. Decide whether to commit it. Note its `-v`
lines predate the change that put `provider=` first, so they no longer match
current output.

## Known gaps and unverified claims

### Verified against the live service

- **`model-router` and the Responses API.** Microsoft's docs show the router
  on the Responses API. Against this resource it returns `400 The requested
  operation is unsupported`, on every host and every region checked. Chat
  Completions is the default for that reason. Worth re-checking after a
  service update; the router's capability list is the tell.
- **Azure CLI account drift.** The CLI's default account changed twice during
  development, once to an unrelated tenant. `qq` now pins the subscription so
  it asks for the right account regardless. Configs written before this need
  `qq config set subscription <id>` or a re-run of `./scripts/setup-cli.sh`.
- **`qq` blocks on an idle stdin pipe.** By design: it is what makes
  `git diff | qq` work. But a harness that leaves stdin open and silent will see
  `qq` wait forever. A `select()` with a short timeout when stdin is a pipe
  would fix it at the cost of racing slow producers. Undecided.

### Relying on undocumented behaviour

- `routing` and `latency_checkpoint` on Azure responses are extensions, not
  OpenAI schema. Everything at `-vvv` that comes from them is read best-effort
  and dropped silently if absent.
- OpenRouter's top-level `provider` field on a successful response is
  undocumented. It is only a fallback when `openrouter_metadata` names no
  provider.
- OpenRouter's OpenAPI spec types `max_price` as strings while the prose says
  numbers. `qq` does not send it yet. Test both if it is ever added.

### Cost figures not available

- Azure does not publish a per-call price for the `web_search` tool. It defers
  to the Bing APIs pricing page.

### Polish

- When Entra sign-in fails, `azure-identity` prints an eight-line chain
  failure to stderr *before* `qq` prints its own one-line error and hint. The
  SDK message is genuinely useful (it names the exact `az login --tenant`
  command) but the presentation is noisy. Quiet the logger and fold the useful
  line into the hint.
- Client overhead is about 1.3s per call, nearly all of it the `openai` SDK
  import. `qq --version` runs in 0.06s because that import is deferred, but a
  real question cannot avoid it. A lighter HTTP path would help; probably not
  worth the maintenance.
- The `router` config value is recorded by `setup-cli.sh`. The inference API
  cannot report what a deployment is backed by, so this reflects setup time and
  goes stale if the deployment is changed underneath it.
- Bicep CLI 0.47.16 is available; 0.46.1 is installed. Harmless warning on every
  deploy.

### Environment, not qq

- A live-looking Azure OpenAI key is exported in the development shell as
  `AZURE_OPENAI_CODEX_API_KEY`. Unrelated to this project and never committed,
  but any child process can read it.
