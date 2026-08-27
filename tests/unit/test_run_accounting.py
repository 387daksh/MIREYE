from app.ai.accounting import finish, record_mireye, record_model, start


def test_run_accounting_records_known_values_without_inventing_prices() -> None:
    token = start("gpt-5.6-sol")
    record_model({"input_tokens": 12, "output_tokens": 4})
    record_mireye(quoted=3, charged=2)
    usage = finish(token)
    assert usage == {
        "model": "gpt-5.6-sol",
        "input_tokens": 12,
        "output_tokens": 4,
        "model_cost": "UNKNOWN",
        "mireye_charged_credits": 2.0,
        "mireye_quoted_credits": 3.0,
        "model_usage_by_module": {"unknown": {"input_tokens": 12, "output_tokens": 4}},
    }
