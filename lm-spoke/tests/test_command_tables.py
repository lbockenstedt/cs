"""Guard the per-domain command tables that route CS_* commands to handlers."""
import inspect

from cs_spoke import CSSpoke

TABLES = ("_AGENT_COMMANDS", "_SIM_COMMANDS", "_CONFIG_COMMANDS",
          "_CLIENT_COMMANDS", "_INGEST_COMMANDS")


def test_every_command_maps_to_an_async_method():
    for table in TABLES:
        for cmd, name in getattr(CSSpoke, table).items():
            method = getattr(CSSpoke, name, None)
            assert method is not None, f"{table}[{cmd}] -> missing {name}"
            assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_command_tables_are_disjoint():
    seen = {}
    for table in TABLES:
        for cmd in getattr(CSSpoke, table):
            assert cmd not in seen, f"{cmd} in both {seen.get(cmd)} and {table}"
            seen[cmd] = table
