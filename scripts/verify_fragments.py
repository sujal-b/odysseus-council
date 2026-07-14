"""Verify fragment extraction preserves prompt content.

Run: python scripts/verify_fragments.py
Exit code 0 = all match, 1 = mismatch found.
"""

import sys
import re
import pathlib

# Add project root to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from council_of_agents.scripts.prompt_composer import PromptComposer


def normalize_blank_lines(text: str) -> str:
    """Normalize newlines: collapse runs to single newline."""
    return re.sub(r"\n{2,}", "\n", text)


def verify():
    composer = PromptComposer()
    roles = [
        "chair", "strategist", "manager", "implementer",
        "implementer_direct", "validator", "validator_task",
    ]
    ok = True
    for role in roles:
        monolithic = composer._load_monolithic(role).replace("{{workspace}}", "")
        monolithic = re.sub(r"\n*<context>.*?</context>\n*", "\n", monolithic, flags=re.DOTALL)
        monolithic = normalize_blank_lines(monolithic)
        composed = normalize_blank_lines(composer.compose(role).replace("{{workspace}}", ""))
        monolithic = monolithic.strip()
        composed = composed.strip()
        if monolithic == composed:
            print(f"  OK  {role} ({len(composed)} chars)")
        else:
            print(f"FAIL  {role}: monolithic={len(monolithic)} chars, composed={len(composed)} chars")
            for i, (a, b) in enumerate(zip(monolithic, composed)):
                if a != b:
                    print(f"      First diff at char {i}:")
                    print(f"      mono: ...{monolithic[max(0,i-30):i+30]}...")
                    print(f"      comp: ...{composed[max(0,i-30):i+30]}...")
                    break
            if len(monolithic) != len(composed):
                shorter = min(len(monolithic), len(composed))
                print(f"      Length diff: mono={len(monolithic)}, comp={len(composed)}")
                if len(monolithic) > shorter:
                    print(f"      Extra in mono: {monolithic[shorter:shorter+80]!r}")
                else:
                    print(f"      Extra in comp: {composed[shorter:shorter+80]!r}")
            ok = False
    return ok


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("Verifying fragment extraction fidelity...")
    result = verify()
    if result:
        print("\nAll roles match.")
    else:
        print("\nMismatches found.")
    sys.exit(0 if result else 1)
