# MCP integration research

Checked 2026-08-30 against official protocol, SDK, tooling, and client documentation.
This is a recommendation report, not a claim that the implementation passes conformance.
No publishing, client registration, or runtime changes were made for this research.

Implementation follow-up: the reliability-only SDK migration adopts Python 3.10+
and default MCP dependencies, keeping the existing ten-tool interface. The audit
below records the original `dc78e5e` behavior, not a description of the migrated
server. Regression coverage now lives in `tests/test_mcp_server.py`. SDK discovery
lists modern revisions; legacy revisions are verified through initialization.
Setup commands, Inspector scripts, registry metadata, and bundles remain deferred.

## Implementation audit

Audited branch `code/feature/improve-parsing-of-rust-and-go` at `dc78e5e`.
The happy path works, but the server has defects even under its advertised
2024-11-05 protocol. It also does not implement the current 2026-07-28 protocol.
These are separate conclusions: legacy support is legitimate, not itself a bug.

### P1: Invalid message shapes terminate the MCP process

`blastradius/mcp_server.py:670` assumes a dictionary, and `:700` assumes dictionary
parameters and a hashable tool name. The receive loop at `:731` catches JSON
syntax errors only. After a valid legacy initialization, each of `null`, `[]`,
`tools/call` with `params: null`, and a list-valued tool name terminated the process
with exit status 1. A following ping received no response. A missing `jsonrpc`
field was accepted as a successful ping.

Validate the envelope before dispatch; return the appropriate invalid-request or
invalid-parameters error and keep processing subsequent messages. Do not confuse
this with a requirement to implement JSON-RPC batch requests. MCP's own message
schema determines which message shapes are supported.
[JSON-RPC request and error rules](https://www.jsonrpc.org/specification),
[MCP base messages](https://modelcontextprotocol.io/specification/2026-07-28/basic)

### P2: Notifications receive invalid responses

Only `notifications/initialized` is suppressed at `blastradius/mcp_server.py:694`.
A `notifications/cancelled` message with no ID instead returned `-32601` with
`id: null` through `:722`. Notification recipients must not respond, including
when a notification method is unknown. A client can mistake these unsolicited
responses for protocol corruption. Notification dispatch and request dispatch
need separate handling; cancellation behavior also needs explicit tests.
[JSON-RPC notification rule](https://www.jsonrpc.org/specification#notification)

### P2: Failed tools can be reported as successful

`get_impact` returns an error dictionary at `blastradius/mcp_server.py:291`, but
the wrapper at `:707` emits it as ordinary text content without `isError: true`.
Reproduced with `missing.py` against a valid fixture index. Similar return paths
exist in dependency, temporal, graph, and changed-since handlers. Exceptions are
flagged correctly, so callers receive inconsistent failure semantics.

Use a consistent tool-error representation, mapping execution failures to
`isError: true`; keep malformed protocol requests distinct from execution errors.
[Legacy tool errors](https://modelcontextprotocol.io/specification/2024-11-05/server/tools#error-handling)

### P2: Advertised input schemas are not enforced

The dispatcher at `blastradius/mcp_server.py:700` passes arguments directly to
handlers. `graph_query(direction="sideways")` returned a successful, misleading
one-node graph despite the declared enum. A string threshold `"5"` was accepted
where the schema requires a number. Validate required fields, types, enum values,
and documented bounds before a handler can read or write indexes.
[Legacy validation requirement](https://modelcontextprotocol.io/specification/2024-11-05/server/tools#security-considerations)

### P2: Explicit repository selection is lost in current-impact queries

`temporal_impact` opens the supplied `db_path`, then at
`blastradius/mcp_server.py:541` resolves the JSON index from the process working
directory instead. From outside the fixture repository, a valid explicit database
and indexed filename failed with "No blastradius.json found". If the working
directory belongs to another indexed repo, this can instead read the wrong graph.
Honor the selected database's repository when locating companion artifacts.

Related source-inspection finding, not separately reproduced: `lookup_symbol`
at `:366` consults the working-directory database before honoring an explicit
`symbol_index_path`. Add a two-repository test with matching symbol names so an
explicit path always wins. Do not fix this by keeping an implicit "last analyzed
repo" shared across requests; prefer explicit repository/artifact selection.

### Current-standard compatibility gap, not a legacy-version violation

`blastradius/mcp_server.py:685` always advertises `2024-11-05`. A modern
`server/discover` request returned method-not-found, and `tools/list` carrying
an unsupported modern version was processed as a legacy request. The wrapper
also lacks modern `resultType` fields. A client supporting both eras can fall
back; a modern-only client cannot use this as a current-standard server.

Implement version-specific behavior and test both eras if current compatibility
is a goal; changing a version string is insufficient. Separately, advertised
`serverInfo.version` is hardcoded to `0.1.0` at `:690`, although package metadata
is `0.3.7`; derive it from installed distribution metadata for useful diagnostics.
[Compatibility matrix](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning#compatibility-matrix),
[discovery requirement](https://modelcontextprotocol.io/specification/2026-07-28/server/discover),
[result types](https://modelcontextprotocol.io/specification/2026-07-28/basic#result-responses)

### Verification performed and test gaps

- `uv run pytest -q`: **48 passed**.
- Exercised the real CLI subprocess over stdio, using copied temporary fixtures,
  captured stdout/stderr, a 15-second subprocess timeout, and cleanup on exit.
  All defects described as reproduced above came from this harness, not mocks.
- Successful analysis indexed the three-file fixture and produced only JSON-RPC
  on stdout; diagnostic output went to stderr. Normal EOF shutdown worked.
- The installed-package MCP test at `tests/test_viz_server.py:271` checks only
  `tools/list`, without initialization or modern metadata. It establishes launcher
  availability, not protocol compliance.
- `benchmark/test_mcp.py:47` blocks on `readline()` without a request timeout, and
  its stderr pipe is not drained during ordinary calls. Its legacy initialization
  checks presence of fields, not version compatibility. Improve this harness
  before relying on it as a CI gate.
- No Inspector/SDK conformance suite, Windows runtime, or exhaustive security
  audit was run. No production server code or existing tests were changed.

## Recommended implementation order

1. Add real-stdio regression tests for the reproduced defects; fix envelope,
   notification, argument, tool-error, and explicit-path handling without changing
   public tool names or the frozen graph export schema.
2. Decide the supported-version matrix and SDK policy, then implement and test
   modern compatibility while preserving explicitly supported legacy clients.
3. Add an optional pinned Inspector smoke script plus installed-wheel/uvx tests
   covering a complete tool round trip, Unicode, EOF, and failure recovery.
4. Add tested client configuration examples or a print-only configuration helper.
   Include explicit repository selection and absolute executable paths where
   GUI environments require them. A diagnostic helper could report executable,
   package/protocol versions, index paths, and a bounded connection check without
   printing credentials or rewriting user configuration.
5. Add honest read/write tool annotations and, for supported revisions,
   structured output schemas. These improve client behavior but are optional
   protocol features. Index-building tools write files; semantic search may use
   a configured external embedding service, so annotations must reflect that.
   [Tool metadata](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool)
6. Prepare optional registry metadata and verify generated launch commands;
   publish only in a separately authorized release. Consider MCPB after this.

## Protocol compliance comes first

The latest dated, released MCP specification is **2026-07-28**, not 2025-11-25.
The July release is final; the separate draft should not be treated as a stable
implementation contract. Its most important change for this project is a stateless
protocol with per-request metadata instead of an initialization handshake.
[Official release announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

Modern servers must implement `server/discover`; clients may skip discovery and
call a tool directly. Unsupported requested protocol versions require error
`-32022` with supported/requested version information. Supporting older,
handshake-based clients is explicitly permitted as a dual-era implementation.
Consequently, simply changing the version string in an `initialize` response
would not implement the new protocol.
[Versioning and compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)

Stdio remains a standard transport: one newline-delimited JSON-RPC message per
line, stdout reserved for protocol messages, diagnostics on stderr, and graceful
exit when stdin closes. No HTTP server, OAuth deployment, container, or registry
entry is necessary to retain a local stdio integration. Modern cancellation also
requires no further messages for a cancelled request.
[Stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)

Recommendation: explicitly document and test supported protocol revisions,
including separate legacy-handshake and modern-request cases if both are claimed.
Do not label a successful handshake or Inspector connection as full conformance.

## Verification scripts worth adding

1. Add a bounded **real-process MCP smoke test** to the normal pytest suite and
   reuse its transport harness from `benchmark/test_mcp.py`. Exercise installed
   `blastradius serve --mcp`, notifications, discovery/initialization as applicable,
   tools listing, successful and unsuccessful tool calls, invalid envelopes and
   arguments, Unicode, clean stdout, and EOF shutdown. Run against a temporary
   fixture repository outside the checkout, with stderr drained and guaranteed
   timeout/cleanup. This is a project test harness, not a protocol requirement.
2. Add an optional maintainer **Inspector check** with a pinned, tested Inspector
   version and a package/executable override. Inspector provides interactive UI
   and scriptable CLI checks, including `tools/list --strict` schema-portability
   lint and JSON output. The observed released-main version is 2.4.0; its Node
   minimum is 22.19.0. Keep Node/Inspector out of runtime dependencies.
   [Inspector](https://github.com/modelcontextprotocol/inspector),
   [CLI options](https://github.com/modelcontextprotocol/inspector/blob/main/clients/cli/README.md),
   [package metadata](https://github.com/modelcontextprotocol/inspector/blob/main/package.json)

For the current Inspector v2 CLI, the complete server command goes **before**
the separator and Inspector options after it; this differs from its web UI.
For example, the following checks the installed executable's catalog:

```sh
npx --yes @modelcontextprotocol/inspector@2.4.0 --cli \
  /absolute/path/to/blastradius serve --mcp -- \
  --method tools/list --strict --format json
```

This command is adapted from the documented argument rules, not executed during
this research. Validate the pinned release when implementing the script.
[Inspector command parsing](https://github.com/modelcontextprotocol/inspector/blob/main/docs/mcp-server-configuration.md)

The official conformance runner is useful reference material, but its documented
**server** mode targets a running URL. Its `--command` option tests a **client**, not
a stdio server. Do not add HTTP merely to run this suite or describe an Inspector
smoke test as official certification.
[Conformance runner](https://github.com/modelcontextprotocol/conformance)

## Installation, configuration, and discoverability

Keep the existing persistent installation path:

```sh
uv tool install blastradius-cli
blastradius serve --mcp
```

Offer the explicit one-off launcher where a client prefers package execution:

```sh
uvx --from blastradius-cli blastradius serve --mcp
```

The `--from` matters because the distribution and executable names differ. A
release-pinned configuration can use `--from blastradius-cli==VERSION`; replace
`VERSION` with an actually published version. Absolute executable paths avoid
depending on a GUI application's PATH. Test both launcher forms against the
built wheel rather than only importing the server from the source checkout.
[uv tool guidance](https://docs.astral.sh/uv/guides/tools/)

Provide small, tested **client-specific configuration examples**, not a supposedly
universal MCP configuration file. Claude Code uses `mcpServers` in `.mcp.json`;
VS Code's workspace file is `.vscode/mcp.json` with a `servers` wrapper. Both
support command/argument arrays for local servers. A future config-printing helper
could prevent quoting and naming errors, but that helper would be project UX,
not an MCP standard; it should print configuration without silently editing user
settings.
[Claude Code configuration](https://code.claude.com/docs/en/mcp),
[VS Code configuration](https://code.visualstudio.com/docs/agent-customization/mcp-servers)

For discoverability, optionally prepare **`server.json` for the official MCP
Registry** using the canonical repository, a GitHub-owned namespace, PyPI
identifier `blastradius-cli`, exact published version, and stdio transport. The
Registry hosts metadata, not package artifacts, and remains in preview.
[Registry quickstart](https://modelcontextprotocol.io/registry/quickstart)

PyPI ownership verification requires an `mcp-name: SERVER_NAME` marker in the
published README/description matching the registry name, for example
`<!-- mcp-name: io.github.IWasZ3r0Cool/blast-radius -->`. Publishing registry
metadata therefore follows a package release that contains the marker.
[PyPI verification](https://modelcontextprotocol.io/registry/package-types)

The registry format supports `runtimeHint`, `runtimeArguments`, and
`packageArguments`, including embedded MCP subcommands. **Do not assume** a naive
PyPI entry correctly launches this package: `blastradius-cli` is not its executable.
Validate the exact consuming client's generated command against the wheel before
publishing metadata; do not invent an unsupported executable field. Registry
publication and any new console alias need a deliberate follow-up decision.
[Registry format](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md)

An **MCPB desktop bundle** is an optional later distribution channel for compatible
hosts. The official format supports a `uv` runtime beginning with manifest 0.4,
using `pyproject.toml` instead of bundled dependencies. This could reduce desktop
setup friction, but adds host-specific packaging and clean-machine tests; it is
not required for protocol compliance or the current PyPI distribution.
[MCPB manifest](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md)

## Python SDK tradeoff

This repository currently promises Python 3.9+ and has no required runtime
dependencies. The official Python SDK's current stable line is v2 and requires
Python 3.10+. It brings an async/runtime and validation dependency stack; v1 is
the previous maintenance line, not a way to preserve Python 3.9 compatibility.
[SDK installation](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/installation.md),
[SDK project metadata](https://github.com/modelcontextprotocol/python-sdk/blob/main/pyproject.toml),
[v1 Python requirement](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/pyproject.toml)

Recommendation: first add SDK-backed interoperability tests in a separate
Python-3.10+ development environment while keeping core compatibility unchanged.
Then choose explicitly between maintaining the small stdlib protocol layer with
versioned wire tests, or migrating MCP to the SDK with an approved Python/runtime
dependency policy change. An optional SDK-backed MCP extra could isolate added
dependencies, but would still need a clear Python floor and a migration path for
existing `serve --mcp` users. Do not silently raise the entire package's minimum.
