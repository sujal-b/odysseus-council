"""Build structured context envelopes for the user message.

All dynamic context (workspace, session state, task info) lives here,
not in the system prompt. The system prompt is 100% static.
"""

import os


def build_context_envelope(
    workspace: str = None,
    repository_context: str = None,
    session_id: str = None,
    complexity: str = None,
    route: str = None,
    skill_context: str = None,
    past_context: str = None,
    success_context: str = None,
    task_description: str = None,
    dependency_outputs: str = None,
    chair_classification: str = None,
    plan_text: str = None,
    debate_context: str = None,
) -> str:
    """Build a structured context block to prepend to the user message.

    Returns empty string if no context is provided.

    ``workspace`` may be a relative label; absolute local paths are omitted
    from provider-bound context. ``repository_context`` is read-only evidence
    that agents must ground plans in and never substitutes for local tool scope.
    """
    sections = []

    if workspace and not os.path.isabs(workspace):
        sections.append(f"<workspace>\n{workspace}\n</workspace>")

    if repository_context:
        sections.append(
            f"<context:repository_capsule>\n{repository_context}\n</context:repository_capsule>"
        )

    state_parts = []
    for k, v in [
        ("session_id", session_id),
        ("complexity", complexity),
        ("route", route),
    ]:
        if v:
            state_parts.append(f"- {k}: {v}")
    if state_parts:
        sections.append(
            "<session_state>\n" + "\n".join(state_parts) + "\n</session_state>"
        )

    for label, val in [
        ("Relevant Skills", skill_context),
        ("Past Outcomes", past_context),
        ("Success Patterns", success_context),
        ("Chair Classification", chair_classification),
        ("Plan", plan_text),
        ("Task", task_description),
        ("Dependency Outputs", dependency_outputs),
        ("Debate Context", debate_context),
    ]:
        if val:
            tag = label.lower().replace(" ", "_")
            sections.append(f"<context:{tag}>\n{val}\n</context:{tag}>")

    return "\n\n".join(sections)
