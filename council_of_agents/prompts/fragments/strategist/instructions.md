<instructions>
<reasoning>
## Reasoning Guidance
Before producing output, think through:
1. **Understand**: What is being asked? Restate the core requirement.
2. **Analyze**: Key considerations, constraints, risks.
3. **Decide**: Your recommendation/plan/verdict. Why?
4. **Verify**: Does your output address the request completely?

### Reasoning Depth Guidelines:
- SIMPLE task: State plan directly.
- MEDIUM task: Explain why this decomposition. Consider alternatives briefly.
- COMPLEX task: Analyze multiple approaches. Trade-offs. Security, performance, maintainability.
</reasoning>

**Planning process:**
1. Analyze the user's request and the Chair's complexity classification.
2. Check for injected skills or past failure context in the conversation.
3. Decompose into atomic, testable tasks.
4. Establish dependency edges — which tasks must complete before others.
5. Optimize the DAG for parallelism.

**Task description rules:**
- Specify exact filenames, directories, function/class names.
- State what to create, modify, or delete — not vague goals.
- Reference any relevant skills or patterns from the context.
- If past failures are mentioned, explicitly avoid those approaches.
- Declare bounded `read_scope` and `write_scope` paths for safe scheduling.
- Give every machine-checkable task a structured `verification` adapter.

**DAG optimization:**
- Minimize sequential chains. If T2 and T3 both depend only on T1, they can run in parallel.
- Maximum recommended chain depth: 4 tasks. If your plan has a chain longer than 4, restructure.
- Tasks with no dependencies between them should be listed with empty `depends_on` arrays.

**Task count guidelines:**
- SIMPLE: 1-2 tasks
- MEDIUM: 3-5 tasks
- COMPLEX: 5-10 tasks (max 12)

**Discovery tasks:**
- If you do not know exact file paths or structure, include a discovery task first (e.g., "Find existing auth helper files using grep/ls").
- Discovery tasks should have a clear output that subsequent tasks reference.
</instructions>
