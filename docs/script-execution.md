# Script execution

`execute_script` runs one Python workflow against a selected same-host Archicad instance. This document is the canonical authoring, result, cancellation, and safety contract for that tool.

## Preconditions

Before executing a script:

1. Save or back up the Archicad model and use a disposable project when exploring unfamiliar commands.
2. Call `list_instances` and select the intended `port`; do not guess a port when several instances are open.
3. Inspect every command contract with `get_docs` and start with read-only commands.
4. Review the complete script, including imports and file, network, subprocess, and model operations.
5. Do not execute code supplied by an untrusted model, prompt, project, file, or other source.

The MCP server, worker, and Archicad instance must share the host loopback interface. The server communicates with Archicad on the selected local port; it does not provide a remote execution transport.

## Authoring a script

The script is the body of an async function. Top-level `await` is therefore valid, and two names are injected:

- `archicad`: an async API object connected to the selected instance;
- `port`: the selected integer Archicad port.

Use `await archicad.command(name, parameters)` for built-in JSON API commands. If `name` does not start with `API.`, the prefix is added automatically. Use `await archicad.tapir(name, parameters)` for Tapir commands; the name is passed without adding `API.`. Both parameter arguments are optional dictionaries.

Assign the final value to `result`. It must be JSON-compatible: an object, array, string, finite number, Boolean, or `None`, composed only of those values. If the script does not assign `result`, the successful result is `null`. Text written to standard output and standard error is captured separately.

Minimal read-only example:

```python
result = await archicad.command("GetProductInfo")
```

A multi-step body can use ordinary async Python:

```python
product = await archicad.command("API.GetProductInfo")
print("Read product information")
result = {"product": product, "port": port}
```

## Result contract

The tool returns a `ScriptResult` with these fields:

| Field | Meaning |
| --- | --- |
| `success` | `true` only when execution and result serialization completed without a stable error. |
| `result` | The normalized JSON-compatible value, or `null` on failure. |
| `stdout` | Text captured from standard output in the worker. |
| `stderr` | Text captured from standard error in the worker. |
| `error` | A human-readable error, or `null` on success. |
| `error_code` | A stable code below, or `null` on success. |
| `execution_model` | `local_user`. |
| `execution_time_ms` | Elapsed execution time in integer milliseconds. |

Stable `error_code` values are:

| Code | Meaning |
| --- | --- |
| `syntax_error` | The submitted Python body could not be compiled. |
| `runtime_error` | Python or an Archicad/Tapir call raised while the body ran. |
| `timeout` | The configured execution timeout elapsed. |
| `worker_start` | The disposable worker process could not be started. |
| `worker_exit` | The worker exited abnormally before returning a result. |
| `worker_protocol` | The worker request or response did not satisfy the internal result contract. |
| `result_not_json` | The assigned `result` was not JSON-compatible. |

A failed Archicad or Tapir command is reported as `runtime_error`; inspect `error` for its diagnostic. Captured output is diagnostic data, not a transaction log or proof that an external effect completed.

## Timeout and cancellation

`timeout_seconds` defaults to 300 seconds. It accepts any positive finite integer or floating-point number. Set it to `null` to disable the execution timeout; zero, negative, infinite, Boolean, and nonnumeric values are rejected.

When the timeout elapses, the server terminates the worker it owns and returns `error_code: "timeout"`. When the MCP transport cancels the call, the server terminates the owned worker and propagates cancellation rather than returning a `ScriptResult`.

Termination is best effort only for the owned worker process. It does not guarantee that a child process spawned by the script is terminated, and it cannot roll back Archicad changes, file writes, network requests, or other external effects already started or completed.

## Authority and isolation boundary

Each script runs in a disposable child process under the same operating-system account as the MCP server. The process boundary provides reliability isolation and lets the server terminate its worker. It is **not a sandbox or permission boundary**.

A script has ordinary same-user Python authority: it can import modules, read or write accessible files, use the network, start processes, and call Archicad or Tapir commands. The server provides no per-script approval or confirmation gate, command allowlist, audit guarantee, model transaction, rollback, or recovery mechanism. Spawned processes and external effects are not guaranteed to stop when the owned worker stops.

The MCP client receives the script result, captured output, and errors. Check the client's data-handling settings before using scripts with sensitive project data.
