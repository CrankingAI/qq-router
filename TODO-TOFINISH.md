# TODO / to finish

State as of 2026-09-18. Everything below is either a decision the maintainer
still has to make, or a known gap. Nothing here blocks daily use: both backends
work end to end, 274 tests pass, CI is green.

## Decisions to make

These are choices, not bugs. Each has a recommendation, but the call is yours.

### 1. Web search — done, 2026-09-15

Shipped as `--search`: Brave Search offered to the model as a function tool on
the Responses API, on both backends. The model decides whether to search and
writes its own query; at most two rounds, then it must answer; a `Sources:`
line on searched answers; off by default; `search=` at `-v`, the queries at
`-vv`. Measured: a question that triggered two searches took 8-9s and ~1,500
input tokens over three requests; one that triggered none made one request,
searched nothing, and paid ~300 extra input tokens for the tool definition and
the search rules (500 in against 200 unsearched).

What changed in the analysis. The table below used to say Azure could not do
row two because `model-router` rejects the Responses API. That was too broad.
It rejects it **on the account endpoint** (`*.openai.azure.com`: still HTTP 400,
and the management-plane capability list agrees, `chatCompletion` and `router`
with no `responses`). Through a Foundry **project** endpoint
(`*.services.ai.azure.com/api/projects/<name>`) the router accepts Responses,
tools included, and Microsoft's own how-to shows exactly that call. So the fix
was one child resource: `infra/modules/ai.bicep` now creates a project,
`deploy.sh` prints its endpoint, `setup-cli.sh` records it as `endpoint`, and
`qq` picks Responses on a project endpoint and Chat Completions on an account
endpoint by itself.

Row four (client-side function calling) is what got built, not row one. A
search that runs on every question was measured to cost 3-4x the tokens and
change nothing on the README's own examples; letting the model decide removed
that tax. Still open: whether `search = true` should become the default once
there is data on how often the model searches when it should not. Also noted:
OpenRouter's credit pre-check returned 402 "requires more credits or fewer
max_tokens" for `cost_tier` plus tools on a low balance; that is the balance,
not a compatibility problem.

The original analysis, kept for the record:

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

### 1b. Getting a question past the shell — done, 2026-09-18

Shipped. The problem: ordinary English is full of shell metacharacters, and the
shell reads a command line before `qq` does, so nothing `qq` parses can undo it.
Measured against zsh: `qq what is a CNAME?` dies at `no matches found`,
`qq what's the difference` hangs on an unmatched quote, `qq what does (x, y) mean`
is a file-attribute error, and — worse, because they are silent — `$PATH`
expands, `#42` truncates the question at the `#`, and `is A > B` creates a file
called `B`.

Three of the four options from the analysis are in:

* **A `qq>` prompt** (`src/qq/repl.py`): a bare `qq` at a terminal, or `qq -i`.
  Deliberately stateless — each line is a fresh question. A conversational REPL
  would need message history in both backends (`Backend.ask` takes one
  `prompt: str`) and would turn a disposable-question tool into a chat client
  with an unpredictable bill. The banner says so out loud, because a prompt
  otherwise invites a follow-up `why?`. readline history is memory-only: a
  dotfile of everything someone asked is not something this tool should create
  behind their back.
* **`alias qq='noglob qq'`** in the README. Measured coverage: fixes `?`, `*`,
  `[`, `(`; does not fix `{a,b}`, `'`, `$`, `#`. One line, no code, and it
  removes the most common case.
* **`qq -e`** (`src/qq/editor.py`): `$EDITOR` for anything long or multi-line.
  Falls back to `nano` before `vi`, since someone who never set `$EDITOR` should
  not need `:wq` to ask a question. Only the two seeded instruction lines are
  stripped, by exact match, so a question containing `#!/bin/sh` survives.
* **`--`**, plus a hint that suggests it, because argparse's "unrecognized
  arguments: -rf do" is true and useless.

Deliberately **not** shipped: a zsh ZLE widget / `accept-line` hook that grabs
the raw buffer. It is the only thing that would fix apostrophes and `$` on a
command line, but it is per-shell code to maintain and invisible magic when it
misfires. Revisit only if the prompt turns out not to absorb the demand.

Also fixed on the way: `tests/conftest.py` now points `QQ_CONFIG_DIR` at an
empty directory. The suite was reading the developer's real
`~/.config/qq/config.toml`, so 15 tests failed on any machine where `qq` was
actually configured (`search = true` in the config reached `resolve()`). CI was
green because CI has no config file, which is the kind of green that hides a
bug.

### 2. Inject today's date into the system prompt

Not implemented. About ten tokens. Removes a class of confidently wrong "as of"
answers; the Azure model guessed the date correctly once, unprompted, which is
luck rather than design. More relevant now that search exists: the tool's rule
is "search when the answer may have changed since your training data", and a
model that knows today's date judges that better.

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

- **`model-router` and the Responses API.** On the account endpoint it returns
  `400 The requested operation is unsupported`, on every host and region
  checked, and the capability list shows `chatCompletion` and `router` without
  `responses`. Through a Foundry project endpoint it works, tools included
  (verified 2026-09-15 against `qq-dev`). `Settings.effective_api` chooses by
  endpoint. Worth re-checking the account route after a service update.
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
  to the Bing APIs pricing page. Moot for now: `--search` uses Brave, priced
  per query on your Brave plan, and no Bing resource exists.

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
