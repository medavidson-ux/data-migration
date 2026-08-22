# -*- coding: utf-8 -*-
"""
sap_migration_agent.py
=======================
A prototype AI agent that helps work through legacy -> SAP field-mapping problems
interactively, grounded in this project's actual artifacts rather than guessing:

  - sap_target_schema/sap_tables.json           (the SAP target table catalog)
  - sap_target_schema/legacy_to_sap_mapping.csv  (table-level routing decisions)
  - ERP_Seed_Data (1).xlsx                        (the real legacy data)
  - etl/transform_engine.py                       (the same transform vocabulary
                                                     used by the specs in etl/specs/)

Run it, describe a mapping problem in plain language (e.g. "how should I map the
Address field on Customers?" or "walk me through Purchase Orders"), and the agent
will look things up, sample real data, propose a mapping using the project's own
transform types, test it against real rows before committing to it, and -- when
you confirm -- persist the decision to etl/agent/mapping_decisions.csv so the
conversation turns into a durable, reusable artifact instead of just chat text.

Requires: pip install anthropic python-dotenv
Credentials: loaded from a .env file (project root or this agent/ folder -- see
.env.example), or resolved automatically by the SDK from the ANTHROPIC_API_KEY
env var / an `ant auth login` profile if no .env file is present. If none of
those provide a key, the first request raises anthropic.AuthenticationError --
see the printed message for how to fix it.

Usage:
    python sap_migration_agent.py

This is a PROTOTYPE: a small, transparent tool loop meant to demonstrate the
pattern, not a hardened production assistant.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import anthropic
import pandas as pd
from dotenv import load_dotenv

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
ETL_DIR = os.path.dirname(AGENT_DIR)
PROJECT_ROOT = os.path.dirname(ETL_DIR)

# Load ANTHROPIC_API_KEY from a .env file if one exists, without overriding a key
# already set in the real environment. Checks this folder first (agent-specific),
# then the project root, so either location works -- see .env.example.
load_dotenv(os.path.join(AGENT_DIR, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

sys.path.insert(0, ETL_DIR)
import transform_engine as eng  # noqa: E402  (reuses the real transform functions)

SCHEMA_DIR = os.path.join(PROJECT_ROOT, "sap_target_schema")
SAP_TABLES_JSON = os.path.join(SCHEMA_DIR, "sap_tables.json")
LEGACY_MAPPING_CSV = os.path.join(SCHEMA_DIR, "legacy_to_sap_mapping.csv")
DECISIONS_CSV = os.path.join(AGENT_DIR, "mapping_decisions.csv")

# Model is configurable via .env (ANTHROPIC_MODEL); base URL too, so any
# Anthropic-compatible endpoint works -- e.g. Zhipu's GLM:
#   ANTHROPIC_API_KEY=<your GLM key>
#   ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
#   ANTHROPIC_MODEL=glm-4.5
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

# Derived from the actual catalog so the tool description the model sees never
# drifts from reality as sap_tables.json grows.
with open(SAP_TABLES_JSON, encoding="utf-8") as _f:
    N_SAP_TABLES = len(json.load(_f))

SYSTEM_PROMPT = """\
You are a migration mapping assistant for an AI-assisted legacy-to-SAP-S/4HANA \
data migration project. You help the engineer work through *specific* field-level \
mapping problems -- not general SAP trivia.

Ground rules:
- Never invent a standard SAP table or field name from memory. Always call \
search_sap_tables first and cite only what it returns. If it returns nothing \
relevant, say so plainly instead of guessing.
- Never invent a real SAP customizing value (a currency key, tax code, GL account \
number, etc.) -- those only exist in the actual target system. When a mapping \
needs one, use the project's own lookup_passthrough convention (an XREF \
placeholder plus a note on what real lookup table it should resolve against \
before load), exactly like the existing specs in etl/specs/ do.
- Before proposing a mapping for a column, look at real sample data for it via \
get_source_sample -- don't reason about a column's shape from its name alone.
- Use lookup_legacy_mapping to check whether the table this column belongs to \
is already routed (MIGRATE/NOT_MIGRATED) in the project's main mapping, and stay \
consistent with that routing.
- Frame every proposal using this project's own transform vocabulary: direct, \
date_yyyymmdd, truncate, constant, lookup_passthrough, row_number_pad, \
signed_amount_pair, split_delimiter, value_map. Don't invent new transform \
types -- if a problem genuinely needs one this vocabulary can't express, say so \
explicitly rather than approximating with the wrong one. Note the distinction \
between lookup_passthrough (defers to an unknown SAP customizing value) and \
value_map (a small, explicitly-known reference table for a fact you can verify \
independently, e.g. geography) -- don't use lookup_passthrough for something \
you can actually confirm, and don't use value_map to paper over something you \
can't.
- Before presenting a mapping as final, call preview_transform to actually run it \
against a few real source rows and show the result -- catch format problems by \
testing, not by inspection alone.
- When the user confirms a mapping is right, call save_mapping_decision to persist \
it. Don't save speculative or rejected proposals.
- Be concise. Show your reasoning briefly, lead with the concrete proposal, and \
flag genuine ambiguity or missing information rather than picking an arbitrary \
answer to sound confident.
"""

TOOLS = [
    {
        "name": "search_sap_tables",
        "description": (
            f"Search the project's SAP S/4HANA target table catalog ({N_SAP_TABLES} tables) by "
            "keyword against table name, description, module, or field names/descriptions. "
            "Always use this before naming any SAP table or field -- never rely on memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword(s) to search for, e.g. 'vendor', 'ACDOCA', 'cost center'."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_legacy_mapping",
        "description": (
            "Look up rows in the project's table-level legacy-to-SAP routing decisions "
            "(legacy_to_sap_mapping.csv) -- shows whether a legacy table migrates, to which "
            "SAP target, at what confidence, and why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "component": {"type": "string", "description": "Filter by domain, e.g. 'HR', 'Sales', 'Finance' (optional)."},
                "table_name_contains": {"type": "string", "description": "Substring to match against the legacy table name (optional)."},
            },
        },
    },
    {
        "name": "get_source_sample",
        "description": (
            "Read real sample rows from a sheet in the legacy seed workbook (ERP_Seed_Data (1).xlsx). "
            "Returns column names, pandas dtypes, and up to n_rows sample records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string", "description": "Exact sheet name, e.g. 'Employees', 'Sales Orders'."},
                "n_rows": {"type": "integer", "description": "Number of sample rows to return (default 5, max 20).", "default": 5},
            },
            "required": ["sheet"],
        },
    },
    {
        "name": "preview_transform",
        "description": (
            "Run a draft field mapping against real rows from a source sheet, using this "
            "project's own transform engine, and return the resulting output rows. Use this "
            "to test a proposed mapping before presenting it as final."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string", "description": "Source sheet name."},
                "target_table": {"type": "string", "description": "SAP target table name, for labeling only."},
                "fields": {
                    "type": "array",
                    "description": "List of field specs, same shape as etl/specs/*.json 'fields' entries.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_field": {"description": "Target field name, or [amount_field, indicator_field] for signed_amount_pair."},
                            "transform": {"type": "string", "enum": ["direct", "date_yyyymmdd", "truncate", "constant", "lookup_passthrough", "row_number_pad", "signed_amount_pair", "split_delimiter", "value_map"]},
                            "source": {"type": "string"},
                            "value": {"type": "string", "description": "For transform=constant."},
                            "lookup_ref": {"type": "string", "description": "For transform=lookup_passthrough."},
                            "max_len": {"type": "integer", "description": "For transform=truncate."},
                            "multiplier": {"type": "integer", "description": "For transform=row_number_pad."},
                            "offset": {"type": "integer", "description": "For transform=row_number_pad -- constant added after the multiply (default 0); use e.g. multiplier=2/offset=0 vs multiplier=2/offset=1 to generate two disjoint key series from one source column."},
                            "width": {"type": "integer", "description": "For transform=row_number_pad."},
                            "debit_source": {"type": "string", "description": "For transform=signed_amount_pair."},
                            "credit_source": {"type": "string", "description": "For transform=signed_amount_pair."},
                            "delimiter": {"type": "string", "description": "For transform=split_delimiter or value_map (default ',')."},
                            "part_index": {"type": "integer", "description": "For transform=split_delimiter or value_map -- 0-based piece to emit/key off."},
                            "mapping": {"type": "object", "description": "For transform=value_map -- the explicit {input: output} reference table.", "additionalProperties": {"type": "string"}},
                            "default": {"type": "string", "description": "For transform=value_map -- value to emit when the input isn't in `mapping` (omit to emit null rather than guess)."},
                        },
                        "required": ["target_field", "transform"],
                    },
                },
                "n_rows": {"type": "integer", "default": 5},
            },
            "required": ["sheet", "target_table", "fields"],
        },
    },
    {
        "name": "save_mapping_decision",
        "description": (
            "Persist a confirmed field-mapping decision to etl/agent/mapping_decisions.csv. "
            "Only call this after the user has confirmed the mapping is correct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_sheet": {"type": "string"},
                "source_column": {"type": "string"},
                "sap_target_table": {"type": "string"},
                "sap_field": {"type": "string"},
                "sap_field_description": {"type": "string"},
                "transform": {"type": "string"},
                "confidence": {"type": "string", "enum": ["HIGH", "APPROX"]},
                "rationale": {"type": "string", "description": "Brief reasoning, in the style of the project's worked-example notes."},
            },
            "required": ["source_sheet", "source_column", "sap_target_table", "sap_field", "transform", "confidence", "rationale"],
        },
    },
]


# --------------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------------

def _load_sap_tables():
    with open(SAP_TABLES_JSON, encoding="utf-8") as f:
        return json.load(f)


def tool_search_sap_tables(query: str) -> str:
    catalog = _load_sap_tables()
    q = query.lower()
    hits = []
    for table_name, meta in catalog.items():
        haystack = " ".join([
            table_name, meta.get("description", ""), meta.get("module", ""), meta.get("type", ""),
            " ".join(f["field"] + " " + f["description"] for f in meta.get("fields", [])),
        ]).lower()
        if q in haystack:
            hits.append({"table": table_name, **meta})
    hits = hits[:8]
    if not hits:
        return json.dumps({"matches": [], "note": "No matches. Do not invent a table name -- report this to the user."})
    return json.dumps({"matches": hits}, indent=2)


def tool_lookup_legacy_mapping(component: str = None, table_name_contains: str = None) -> str:
    df = pd.read_csv(LEGACY_MAPPING_CSV)
    if component:
        df = df[df["component"].str.lower() == component.lower()]
    if table_name_contains:
        df = df[df["legacy_table"].str.lower().str.contains(table_name_contains.lower(), na=False)]
    if df.empty:
        return json.dumps({"matches": [], "note": "No matching rows in legacy_to_sap_mapping.csv."})
    return df.head(25).to_json(orient="records", indent=2)


def tool_get_source_sample(sheet: str, n_rows: int = 5) -> str:
    n_rows = max(1, min(int(n_rows or 5), 20))
    try:
        df = eng.load_sheet(sheet)
    except Exception as e:
        return json.dumps({"error": f"Could not read sheet {sheet!r}: {e}"})
    return json.dumps({
        "sheet": sheet,
        "row_count": len(df),
        "columns": [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns],
        "sample_rows": json.loads(df.head(n_rows).to_json(orient="records")),
    }, indent=2, default=str)


def tool_preview_transform(sheet: str, target_table: str, fields: list, n_rows: int = 5) -> str:
    try:
        df = eng.load_sheet(sheet)
    except Exception as e:
        return json.dumps({"error": f"Could not read sheet {sheet!r}: {e}"})

    n_rows = max(1, min(int(n_rows or 5), 20))
    pending_lookups = defaultdict(set)
    run_log = {"truncations": 0, "date_failures": 0, "date_failure_values": set()}

    out_rows = []
    errors = []
    for _, row in df.head(n_rows).iterrows():
        out_row = {}
        for field_spec in fields:
            try:
                out_row.update(eng.apply_field(row, field_spec, pending_lookups, run_log, target_table))
            except Exception as e:
                errors.append(f"field {field_spec.get('target_field')}: {e}")
        out_rows.append(out_row)

    return json.dumps({
        "target_table": target_table,
        "rows_previewed": len(out_rows),
        "output_rows": out_rows,
        "pending_lookups_triggered": {f"{k[1]} ({k[2]})": len(v) for k, v in pending_lookups.items()},
        "truncations": run_log["truncations"],
        "date_parse_failures": {
            "count": run_log["date_failures"],
            "distinct_values": sorted(run_log["date_failure_values"]),
        },
        "errors": errors,
    }, indent=2, default=str)


def tool_save_mapping_decision(**kwargs) -> str:
    is_new = not os.path.exists(DECISIONS_CSV)
    fieldnames = ["timestamp", "source_sheet", "source_column", "sap_target_table",
                  "sap_field", "sap_field_description", "transform", "confidence", "rationale"]
    with open(DECISIONS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            w.writeheader()
        row = {"timestamp": datetime.now().isoformat(timespec="seconds")}
        row.update({k: kwargs.get(k, "") for k in fieldnames if k != "timestamp"})
        w.writerow(row)
    return json.dumps({"status": "saved", "file": DECISIONS_CSV})


DISPATCH = {
    "search_sap_tables": lambda i: tool_search_sap_tables(**i),
    "lookup_legacy_mapping": lambda i: tool_lookup_legacy_mapping(**i),
    "get_source_sample": lambda i: tool_get_source_sample(**i),
    "preview_transform": lambda i: tool_preview_transform(**i),
    "save_mapping_decision": lambda i: tool_save_mapping_decision(**i),
}


def execute_tool(name: str, tool_input: dict) -> str:
    try:
        return DISPATCH[name](tool_input)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# --------------------------------------------------------------------------------
# Agentic loop (manual loop -- see claude-api skill: avoids the tool-runner beta
# dependency and keeps full control over the multi-turn REPL history)
# --------------------------------------------------------------------------------

def run_turn(client: anthropic.Anthropic, messages: list) -> str:
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            thinking={"type": "adaptive"},
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"  → {block.name}({json.dumps(block.input)})")
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        messages.append({"role": "assistant", "content": response.content})
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "\n".join(text_parts)


def submit_user_turn(client: anthropic.Anthropic, messages: list, user_input: str) -> str | None:
    """Append the user's input and run it to completion.

    On a recoverable API error, truncates `messages` back to where the turn
    started so no half-answered (or unanswered) turn is left behind -- a
    dangling user message or a trailing tool_result would break the required
    role alternation on the next request. Returns the assistant's reply text,
    or None if the turn failed recoverably (caller should just prompt again).
    Raises anthropic.AuthenticationError to signal "stop the REPL".
    """
    turn_start = len(messages)
    messages.append({"role": "user", "content": user_input})
    try:
        reply = run_turn(client, messages)
    except anthropic.AuthenticationError:
        raise
    except anthropic.RateLimitError as e:
        retry_after = e.response.headers.get("retry-after", "a bit")
        print(f"\nRate limited. Retry after {retry_after}s.\n")
        del messages[turn_start:]
        return None
    except anthropic.APIStatusError as e:
        print(f"\nAPI error ({e.status_code}): {e.message}\n")
        del messages[turn_start:]
        return None
    except anthropic.APIConnectionError:
        print("\nNetwork error reaching the Anthropic API. Check your connection.\n")
        del messages[turn_start:]
        return None
    return reply


def main():
    try:
        client = anthropic.Anthropic(
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),  # None -> SDK default
        )
    except Exception as e:
        print(f"Could not initialize the Anthropic client: {e}")
        sys.exit(1)
    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "api.anthropic.com")
    print(f"Model: {MODEL}  via  {endpoint}")

    print("=" * 78)
    print("SAP Migration Mapping Assistant (prototype)")
    print("=" * 78)
    print("Grounded in this project's real schema catalog, legacy data, and transform")
    print("engine. Ask about a specific mapping problem, e.g.:")
    print('  "How should I map the Address field on Customers?"')
    print('  "Walk me through mapping Purchase Orders."')
    print('  "Is Vendor_Rating on Vendor Ratings already covered anywhere?"')
    print("Type 'exit' or Ctrl+C to quit.\n")

    messages: list = []
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        try:
            reply = submit_user_turn(client, messages, user_input)
        except anthropic.AuthenticationError:
            print(
                "\nAuthentication failed. Copy .env.example to .env in this folder and fill "
                "in ANTHROPIC_API_KEY, set the env var directly, or run `ant auth login` to "
                "store a credential profile the SDK will pick up automatically.\n"
            )
            break
        if reply is None:
            continue

        print(f"\nagent> {reply}\n")


if __name__ == "__main__":
    main()
