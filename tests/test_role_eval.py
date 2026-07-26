import asyncio

from council_of_agents.scripts.role_eval import build_messages, evaluate


CHAIR = '{"complexity":"COMPLEX","route":"PIPELINE","action":"write","target":"app","reason":"multi-file"}'
STRATEGIST = '{"tasks":[{"id":"T1","description":"Create app","write_scope":["src/"]}]}'
MANAGER_REVISE = '{"verdict":"REVISE","confidence":0.4,"summary":"Missing test coverage","issues":[{"severity":"warning","task_id":"T1","description":"No tests","suggestion":"Add tests","evidence":"T1 has no test task or verification"}]}'


def test_strategist_uses_only_the_validated_chair_contract():
    messages = build_messages("strategist", "Build an app", chair_reply=CHAIR)

    assert messages[-1]["role"] == "user"
    assert "complexity: COMPLEX" in messages[-1]["content"]
    assert "Return the Strategist JSON contract now." in messages[-1]["content"]


def test_chair_evaluation_records_a_strict_contract():
    async def fake_call(**kwargs):
        assert kwargs["messages"][0]["role"] == "system"
        return CHAIR

    result = asyncio.run(evaluate(
        "chair", "Build an app", endpoint="https://example.test/v1/chat/completions",
        model="test", call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["request_chars"] > 0
    assert result["response_chars"] == len(CHAIR)


def test_manager_receives_the_validated_chair_and_strategist_handoffs():
    messages = build_messages("manager", "Build an app", chair_reply=CHAIR, strategist_reply=STRATEGIST)

    assert messages[-1]["role"] == "user"
    assert "Chair decision data:" in messages[-1]["content"]
    assert STRATEGIST in messages[-1]["content"]


def test_strategist_revision_receives_one_manager_review():
    messages = build_messages(
        "strategist", "Build an app", chair_reply=CHAIR, strategist_reply=STRATEGIST,
        manager_reply=MANAGER_REVISE,
    )

    assert "Previous Strategist plan:" in messages[-2]["content"]
    assert "Manager requested one plan revision." in messages[-1]["content"]
    assert MANAGER_REVISE in messages[-1]["content"]
