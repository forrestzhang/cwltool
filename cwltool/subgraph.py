import urllib
from collections.abc import Mapping, MutableMapping, MutableSequence
from typing import Any, NamedTuple, Optional, Union, cast

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .context import LoadingContext
from .load_tool import load_tool, make_tool
from .process import Process
from .utils import CWLObjectType, aslist
from .workflow import Workflow, WorkflowStep


class _Node(NamedTuple):
    up: list[str]
    down: list[str]
    type: Optional[str]


UP = "up"
DOWN = "down"
INPUT = "input"
OUTPUT = "output"
STEP = "step"


def _subgraph_visit(
    current: str,
    nodes: MutableMapping[str, _Node],
    visited: set[str],
    direction: str,
) -> None:
    if current in visited:
        return
    visited.add(current)

    if direction == DOWN:
        d = nodes[current].down
    if direction == UP:
        d = nodes[current].up
    for c in d:
        _subgraph_visit(c, nodes, visited, direction)


def _declare_node(nodes: dict[str, _Node], nodeid: str, tp: Optional[str]) -> _Node:
    """
    Record the given nodeid in the graph.

    If the nodeid is already present, but its type is unset, set it.
    :returns: The Node tuple (even if already present in the graph).
    """
    if nodeid in nodes:
        n = nodes[nodeid]
        if n.type is None:
            nodes[nodeid] = _Node(n.up, n.down, tp)
    else:
        nodes[nodeid] = _Node([], [], tp)
    return nodes[nodeid]


def find_step(
    steps: list[WorkflowStep], stepid: str, loading_context: LoadingContext
) -> tuple[Optional[CWLObjectType], Optional[WorkflowStep]]:
    """Find the step (raw dictionary and WorkflowStep) for a given step id."""
    for st in steps:
        st_tool_id = st.tool["id"]
        if st_tool_id == stepid:
            return st.tool, st
        if stepid.startswith(st_tool_id):
            run: Union[str, Process, CWLObjectType] = st.tool["run"]
            if isinstance(run, Workflow):
                result, st2 = find_step(
                    run.steps, stepid[len(st.tool["id"]) + 1 :], loading_context
                )
                if result:
                    return result, st2
            elif isinstance(run, CommentedMap) and run["class"] == "Workflow":
                process = make_tool(run, loading_context)
                if isinstance(process, Workflow):
                    suffix = stepid[len(st.tool["id"]) + 1 :]
                    prefix = process.tool["id"]
                    if "#" in prefix:
                        sep = "/"
                    else:
                        sep = "#"
                    adj_stepid = f"{prefix}{sep}{suffix}"
                    result2, st3 = find_step(
                        process.steps,
                        adj_stepid,
                        loading_context,
                    )
                    if result2:
                        return result2, st3
            elif isinstance(run, str):
                process = load_tool(run, loading_context)
                if isinstance(process, Workflow):
                    suffix = stepid[len(st.tool["id"]) + 1 :]
                    prefix = process.tool["id"]
                    if "#" in prefix:
                        sep = "/"
                    else:
                        sep = "#"
                    adj_stepid = f"{prefix}{sep}{suffix}"
                    result3, st4 = find_step(process.steps, adj_stepid, loading_context)
                    if result3:
                        return result3, st4
    return None, None


def get_subgraph(
    roots: MutableSequence[str], tool: Workflow, loading_context: LoadingContext
) -> CommentedMap:
    """Extract the subgraph for the given roots."""
    if tool.tool["class"] != "Workflow":
        raise Exception("Can only extract subgraph from workflow")

    nodes: dict[str, _Node] = {}

    for inp in tool.tool["inputs"]:
        _declare_node(nodes, inp["id"], INPUT)

    for out in tool.tool["outputs"]:
        _declare_node(nodes, out["id"], OUTPUT)
        for i in aslist(out.get("outputSource", CommentedSeq)):
            # source is upstream from output (dependency)
            nodes[out["id"]].up.append(i)
            # output is downstream from source
            _declare_node(nodes, i, None)
            nodes[i].down.append(out["id"])

    for st in tool.tool["steps"]:
        step = _declare_node(nodes, st["id"], STEP)
        for i in st["in"]:
            if "source" not in i:
                continue
            for src in aslist(i["source"]):
                # source is upstream from step (dependency)
                step.up.append(src)
                # step is downstream from source
                _declare_node(nodes, src, None)
                nodes[src].down.append(st["id"])
        for out in st["out"]:
            if isinstance(out, Mapping) and "id" in out:
                out = out["id"]
            # output is downstream from step
            step.down.append(out)
            # step is upstream from output
            _declare_node(nodes, out, None)
            nodes[out].up.append(st["id"])

    # Find all the downstream nodes from the starting points
    visited_down: set[str] = set()
    for r in roots:
        if nodes[r].type == OUTPUT:
            _subgraph_visit(r, nodes, visited_down, UP)
        else:
            _subgraph_visit(r, nodes, visited_down, DOWN)

    # Now make sure all the nodes are connected to upstream inputs
    visited: set[str] = set()
    rewire: dict[str, tuple[str, CWLObjectType]] = {}
    for v in visited_down:
        visited.add(v)
        if nodes[v].type in (STEP, OUTPUT):
            for u in nodes[v].up:
                if u in visited_down:
                    continue
                if nodes[u].type == INPUT:
                    visited.add(u)
                else:
                    # rewire
                    df = urllib.parse.urldefrag(u)
                    rn = str(df[0] + "#" + df[1].replace("/", "_"))
                    if nodes[v].type == STEP:
                        wfstep = find_step(tool.steps, v, loading_context)[0]
                        if wfstep is not None:
                            for inp in cast(MutableSequence[CWLObjectType], wfstep["inputs"]):
                                if "source" in inp and u in cast(CWLObjectType, inp["source"]):
                                    rewire[u] = (rn, cast(CWLObjectType, inp["type"]))
                                    break
                        else:
                            raise Exception("Could not find step %s" % v)

    extracted = CommentedMap()
    for f in tool.tool:
        if f in ("steps", "inputs", "outputs"):
            extracted[f] = CommentedSeq()
            for i in tool.tool[f]:
                if i["id"] in visited:
                    if f == "steps":
                        for in_port in i["in"]:
                            if "source" not in in_port:
                                continue
                            if isinstance(in_port["source"], MutableSequence):
                                in_port["source"] = CommentedSeq(
                                    [rewire[s][0] for s in in_port["source"] if s in rewire]
                                )
                            elif in_port["source"] in rewire:
                                in_port["source"] = rewire[in_port["source"]][0]
                    extracted[f].append(i)
        else:
            extracted[f] = tool.tool[f]

    for rv in rewire.values():
        extracted["inputs"].append(CommentedMap({"id": rv[0], "type": rv[1]}))

    return extracted


def get_step(tool: Workflow, step_id: str, loading_context: LoadingContext) -> CommentedMap:
    """Extract a single WorkflowStep for the given step_id."""
    extracted = CommentedMap()

    step = find_step(tool.steps, step_id, loading_context)[0]
    if step is None:
        raise Exception(f"Step {step_id} was not found")

    new_id, step_name = cast(str, step["id"]).rsplit("#")

    extracted["steps"] = CommentedSeq([step])
    extracted["inputs"] = CommentedSeq()
    extracted["outputs"] = CommentedSeq()

    for in_port in cast(list[CWLObjectType], step["in"]):
        name = "#" + cast(str, in_port["id"]).split("#")[-1].split("/")[-1]
        inp: CWLObjectType = {"id": name, "type": "Any"}
        if "default" in in_port:
            inp["default"] = in_port["default"]
        extracted["inputs"].append(CommentedMap(inp))
        in_port["source"] = name
        if "linkMerge" in in_port:
            del in_port["linkMerge"]

    for outport in cast(list[Union[str, Mapping[str, Any]]], step["out"]):
        if isinstance(outport, Mapping):
            outport_id = cast(str, outport["id"])
        else:
            outport_id = outport
        name = outport_id.split("#")[-1].split("/")[-1]
        extracted["outputs"].append(
            {
                "id": name,
                "type": "Any",
                "outputSource": f"{new_id}#{step_name}/{name}",
            }
        )

    for f in tool.tool:
        if f not in ("steps", "inputs", "outputs"):
            extracted[f] = tool.tool[f]
    extracted["id"] = new_id
    if "cwlVersion" not in extracted:
        extracted["cwlVersion"] = tool.metadata["cwlVersion"]
    return extracted


def get_process(
    tool: Workflow, step_id: str, loading_context: LoadingContext
) -> tuple[Any, WorkflowStep]:
    """Find the underlying Process for a given Workflow step id."""
    if loading_context.loader is None:
        raise Exception("loading_context.loader cannot be None")
    raw_step, step = find_step(tool.steps, step_id, loading_context)
    if raw_step is None or step is None:
        raise Exception(f"Step {step_id} was not found")

    run: Union[str, Any] = raw_step["run"]

    if isinstance(run, str):
        process = loading_context.loader.idx[run]
    else:
        process = run
    return process, step
