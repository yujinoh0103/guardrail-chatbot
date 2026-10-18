"""Load wiring and validate it before any node runs."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import yaml

from .node import Node, resolve
from .state import INITIAL_KEYS


def comparison_variants(config):
    """Build condition-specific pipelines; each is run with its own dialogues/history."""
    comparison = config.get("comparison")
    if comparison is None:
        return [(None, Pipeline.from_dict(config))]
    if not isinstance(comparison, dict):
        raise ValueError("comparison must be a mapping")
    conditions = comparison.get("conditions", [])
    allowed = {"baseline", "label", "category", "rationale", "brief", "explanation"}
    if not isinstance(conditions, list) or not conditions or not all(isinstance(c, str) and c in allowed for c in conditions) or len(set(conditions)) != len(conditions):
        raise ValueError("comparison requires unique supported conditions")
    bot = comparison.get("node", "bot")
    guard = comparison.get("guard_node", "g11")
    feedback = comparison.get("feedback_node", "feedback")
    if len({bot, guard, feedback}) != 3 or any(name not in config.get("nodes", {}) for name in (bot, guard, feedback)):
        raise ValueError("comparison requires distinct chatbot, guard and feedback nodes")
    if config["nodes"][bot]["use"] != "chatbot/default":
        raise ValueError("comparison node must use chatbot/default")
    variants = []
    for condition in conditions:
        variant = deepcopy(config)
        variant.pop("comparison")
        removed = {guard, feedback} if condition == "baseline" else {feedback} if condition in ("label", "category") else set()
        removed_outputs = {variant["nodes"][name]["out"] for name in removed}
        for name in removed:
            del variant["nodes"][name]
        spec = variant["nodes"][bot]
        spec.setdefault("params", {})["condition"] = condition
        spec["in"] = [key for key in spec["in"] if key.removesuffix("?") not in removed_outputs]
        flow = []
        for item in variant["flow"]:
            if isinstance(item, list):
                remaining = [name for name in item if name not in removed]
                if remaining:
                    flow.append(remaining)
            elif item not in removed:
                flow.append(item)
        variant["flow"] = flow
        variant["outputs"] = [key for key in variant.get("outputs", []) if key not in removed_outputs]
        variants.append((condition, Pipeline.from_dict(variant)))
    return variants


@dataclass(frozen=True)
class NodeSpec:
    use: str
    inputs: tuple[str, ...]
    output: str
    params: dict
    router: bool
    factory: type[Node]

    def create(self) -> Node:
        return self.factory(**deepcopy(self.params))


@dataclass(frozen=True)
class Pipeline:
    nodes: dict[str, NodeSpec]
    flow: tuple[tuple[str, ...], ...]
    parallel: tuple[bool, ...]
    positions: dict[str, int]
    final: str
    max_steps: int
    response_mode: str = "generate"
    context_messages: int = 5
    outputs: tuple[str, ...] = ()
    replay_users: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "Pipeline":
        with Path(path).open() as stream:
            return cls.from_dict(yaml.safe_load(stream))

    @classmethod
    def from_dict(cls, config: dict) -> "Pipeline":
        if not isinstance(config, dict):
            raise ValueError("Pipeline must be a mapping")
        declarations = config.get("nodes")
        raw_flow = config.get("flow")
        if not isinstance(declarations, dict) or not declarations:
            raise ValueError("nodes must be a nonempty mapping")
        if not isinstance(raw_flow, list) or not raw_flow:
            raise ValueError("flow must be a nonempty list")
        nodes = {}
        for name, spec in declarations.items():
            if not isinstance(name, str) or not name or name == "END":
                raise ValueError("Node names must be nonempty strings other than END")
            if not isinstance(spec, dict):
                raise ValueError(f"Node {name} must be a mapping")
            use, inputs, output = spec.get("use"), spec.get("in", []), spec.get("out")
            params, kind = spec.get("params", {}), spec.get("type", "node")
            if not isinstance(use, str) or not use:
                raise ValueError(f"Node {name} requires use")
            if not isinstance(inputs, list) or not all(isinstance(k, str) and k.rstrip("?") and "?" not in k[:-1] for k in inputs):
                raise ValueError(f"Invalid inputs for {name}")
            if len({k.removesuffix("?") for k in inputs}) != len(inputs):
                raise ValueError(f"Duplicate inputs for {name}")
            if not isinstance(output, str) or not output or "?" in output or output in INITIAL_KEYS:
                raise ValueError(f"Invalid or reserved output for {name}: {output}")
            if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
                raise ValueError(f"params for {name} must be a string-keyed mapping")
            if kind not in ("node", "router"):
                raise ValueError(f"Unknown node type for {name}: {kind}")
            if output == "next" and kind != "router":
                raise ValueError(f"Only a router can write next: {name}")
            factory = resolve(use)
            node = NodeSpec(use, tuple(inputs), output, deepcopy(params), kind == "router", factory)
            try:
                node.create()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid params for {name}: {exc}") from exc
            nodes[name] = node

        flow, parallel, positions = [], [], {}
        available = set(INITIAL_KEYS)
        for index, item in enumerate(raw_flow):
            if isinstance(item, str):
                group = (item,)
            elif isinstance(item, list) and item and all(isinstance(n, str) for n in item):
                group = tuple(item)
            else:
                raise ValueError(f"Invalid flow item at {index}")
            for name in group:
                if name not in nodes:
                    raise ValueError(f"Unknown flow node: {name}")
                if name in positions:
                    raise ValueError(f"Node appears more than once in flow: {name}")
                positions[name] = index
            outputs = [nodes[name].output for name in group]
            if len(set(outputs)) != len(outputs):
                raise ValueError(f"Parallel nodes share an output at step {index}")
            if isinstance(item, list) and any(nodes[name].router for name in group):
                raise ValueError("Routers must be sequential flow items")
            for name in group:
                for key in nodes[name].inputs:
                    if not key.endswith("?") and key not in available:
                        raise ValueError(f"Input {key} for {name} is unavailable before step {index}")
            available.update(outputs)
            flow.append(group)
            parallel.append(isinstance(item, list))
        unused = set(nodes) - set(positions)
        if unused:
            raise ValueError(f"All declared nodes must occur in flow: {sorted(unused)}")
        final = config.get("final")
        if not isinstance(final, str) or final not in {node.output for node in nodes.values()}:
            raise ValueError("final must be written by a node")
        limits = config.get("limits", {})
        if not isinstance(limits, dict):
            raise ValueError("limits must be a mapping")
        max_steps = limits.get("max_steps", 100)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        input_config = config.get("input", {})
        if not isinstance(input_config, dict):
            raise ValueError("input must be a mapping")
        response_mode = input_config.get("response_mode", "generate")
        context_messages = input_config.get("context_messages", 5)
        replay_users = input_config.get("replay_users", False)
        if not isinstance(replay_users, bool) or (replay_users and response_mode != "generate"):
            raise ValueError("replay_users requires a boolean and response_mode generate")
        if response_mode not in ("generate", "recorded"):
            raise ValueError("response_mode must be generate or recorded")
        if response_mode == "recorded" and any(spec.use.startswith("chatbot/") for spec in nodes.values()):
            raise ValueError("recorded pipelines cannot contain chatbot generation nodes")
        if isinstance(context_messages, bool) or not isinstance(context_messages, int) or context_messages <= 0:
            raise ValueError("context_messages must be a positive integer")
        outputs = config.get("outputs", [])
        if not isinstance(outputs, list) or not all(isinstance(key, str) and key in available for key in outputs):
            raise ValueError("outputs must list available state keys")
        if len(set(outputs)) != len(outputs):
            raise ValueError("outputs must not contain duplicate keys")
        return cls(nodes, tuple(flow), tuple(parallel), positions, final, max_steps,
                   response_mode, context_messages, tuple(outputs), replay_users)
