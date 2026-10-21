"""The plugin contract as a document (docs/plugin_contract.md), generated from `spec/plugins/kinds.json` and the interfaces in `plugin_api`, so that what is written, what is enforced and what is implemented cannot drift apart."""
from __future__ import annotations
import inspect
from .plugin_api import ERROR_CODES, INTERFACES
from .plugins import KINDS, SPEC
import json

_RAW = json.loads(SPEC.read_text())


def _type(s: dict) -> str:
    if "enum" in s: return " | ".join(map(str, s["enum"]))
    t = s.get("type", "any")
    if t == "array": return f"list[{_type(s['items'])}]" if s.get("items") else "list"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "dict"}.get(t, t)


def render() -> str:
    out = ["# Plugin contract", "", "*Generated from `spec/plugins/kinds.json` and `harness/plugin_api.py` by `harness plugins --contract`. Do not edit by hand; `tests/test_plugin_contract.py` fails if it is out of date.*", "",
           "A plugin is an implementation of one of the interfaces below. A deployment names the implementation class in its configuration (`plugins.load[].entry.class`, as `package.module:ClassName`, with `entry.config` as its constructor arguments); the harness loads it, checks that it implements the kind's interface, calls every method with its sample input and checks the answer against the contract, and then calls it only through that interface. A class that leaves a method unimplemented cannot be created.", "",
           "## How errors work", "",
           "A plugin signals an **expected** failure by raising `PluginRejection(code)` with one of the codes below, and only with a code that the method declares. The harness turns what happens into one of its own exceptions:", "",
           "| What happens | The harness throws | Counts toward quarantine |", "|---|---|---|",
           "| The plugin raises a declared code | `PluginRejected` (`reason` = the code, `retryable`) | No, except `unavailable` |", "| The plugin raises a code the method does not declare | `ContractViolation` | Yes |",
           "| The plugin raises any other exception | `PluginFailed` | Yes |", "| The plugin returns the wrong shape | `ContractViolation` | Yes |", "| The plugin is too slow | `PluginTimeout` | Yes |",
           "| The harness's caller sends a bad request | `ContractViolation`, before the plugin is called | No |", "| No active plugin of the kind, or it is quarantined | `PluginUnavailable` | n/a |", "| The run's tool budget cannot cover the call | `PluginBudgetExhausted` | n/a |",
           "| At load: a bad manifest, a refused profile, a class that does not implement the interface, a failed conformance run | `ManifestInvalid`, `PolicyDenied`, `ConformanceFailed` | n/a |", "",
           "Three consecutive faults quarantine a plugin, and a success resets the count. Arguments are validated **before** any resource is looked up, so `invalid_argument` comes before `not_found`.", "",
           "| Code | Retryable | Meaning |", "|---|---|---|"]
    for c, v in _RAW["error_codes"].items(): out.append(f"| `{c}` | {'yes' if v['retryable'] else 'no'} | {v['meaning']} |")
    out += ["", "## Probes", "", _RAW["probe_conventions"]["rule"], "", "Reserved ids: " + "; ".join(f"{k} `{v}`" for k, v in _RAW["probe_conventions"]["reserved_ids"].items()) + ". With `plugins.verify_errors: true`, the host runs every probe when it loads a plugin and refuses a plugin that does not raise exactly the declared error.", ""]
    for kind in sorted(KINDS):
        k, cls = KINDS[kind], INTERFACES[kind]
        out += [f"## `{kind}`: `{cls.__name__}`", "", k["summary"], "", f"Interface version {k['interface_version']}; needs a **{k['min_profile']}** deployment or stricter; the plugin may hold the grants {k['grants'] or 'none'}; its answers are `{k['trust'].split('_')[0]}`.", ""]
        for name, ms in k["methods"].items():
            params = ", ".join(f"{p}: {_type(s)}" for p, s in ms["input"]["properties"].items()); rt = ms["output"]["properties"]
            out += [f"### `{cls.__name__}.{name}({params})`", "", ms["purpose"], "", f"**Returns** {ms['returns_doc']}", "", "| Field | Type |", "|---|---|"] + [f"| `{f}` | {_type(s)}{' (optional)' if f not in ms['output']['required'] else ''} |" for f, s in rt.items()]
            out += ["", f"Effect: {ms['effect'].replace('_', ' ')}. Timeout: {ms['timeout_s']} s." + ("" if ms["errors"] else " Raises nothing."), ""]
            if ms["errors"]: out += ["| May raise | When | Probe |", "|---|---|---|"] + [f"| `{e['code']}` | {e['when']} | {('`' + json.dumps(e['probe']) + '`') if e['probe'] is not None else 'none (cannot be triggered safely)'} |" for e in ms["errors"]] + [""]
    return "\n".join(out).rstrip() + "\n"
