from toolbench import tools as tools_mod

from .conftest import FIXTURE


async def test_fixture_matches_the_live_mcp_server():
    # The tests score against a saved copy of the tool list. If commands.yaml
    # or the server changes, this fails and the fixture needs regenerating:
    #   uv run toolbench tools --out tests/fixtures/tools.json
    live = await tools_mod.fetch_from_mcp()
    assert live == tools_mod.load(FIXTURE)


def test_mutating_tools_require_a_node(tools, specs):
    for t in tools:
        if t.name in specs and specs[t.name].mutating:
            assert "node" in t.input_schema.get("required", []), t.name
