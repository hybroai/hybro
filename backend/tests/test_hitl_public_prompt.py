from common.public_prompt import (
    concrete_agent_input_prompt,
    is_internal_agent_contract_prompt,
)


def test_internal_agent_contract_prompt_is_not_public_hitl():
    json_prompt = (
        "Send a JSON object (directly or in a DataPart) containing client and "
        "requested_coverage. Include client.name and requested_coverage.limit."
    )
    dotted_prompt = (
        "Provide client.name, client.employee_count, and claims_history.prior_claims."
    )
    short_json_prompt = "Return JSON containing client.name."
    structured_prompt = (
        "Provide a structured payload containing client and requested_coverage."
    )

    for prompt in (
        json_prompt,
        dotted_prompt,
        short_json_prompt,
        structured_prompt,
    ):
        assert is_internal_agent_contract_prompt(prompt)
        assert concrete_agent_input_prompt(prompt) is None


def test_friendly_agent_question_remains_public_hitl():
    prompt = "What is Acme SaaS Inc.'s annual revenue?"

    assert not is_internal_agent_contract_prompt(prompt)
    assert concrete_agent_input_prompt(prompt) == prompt
