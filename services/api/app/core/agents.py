from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    objective: str
    outputs: tuple[str, ...]


AGENTS = {
    "showrunner": AgentDefinition(
        "Executive Showrunner",
        "Translate a creative brief into a globally coherent production objective and arbitrate trade-offs.",
        ("creative objective", "budget policy", "final approval"),
    ),
    "story": AgentDefinition(
        "Story Architect",
        "Build the story bible, dramatic beats, dialogue, and typed StoryIR.",
        ("StoryIR", "beat graph", "screenplay"),
    ),
    "visual": AgentDefinition(
        "Visual Director",
        "Convert StoryIR into storyboards, character references, camera plans, and shot contracts.",
        ("storyboards", "shot contracts", "visual rules"),
    ),
    "production": AgentDefinition(
        "Production Manager",
        "Route models, allocate budget, schedule jobs, and recover external API failures.",
        ("render jobs", "production ledger", "asset audit_trail"),
    ),
    "continuity": AgentDefinition(
        "Continuity Supervisor",
        "Inspect every clip against its shot contract and prescribe the minimum-cost repair.",
        ("quality report", "repair instruction", "acceptance decision"),
    ),
    "editor": AgentDefinition(
        "Picture Editor",
        "Assemble accepted shots, dialogue, captions, and final delivery formats.",
        ("edit decision list", "captions", "final master"),
    ),
}
