from app.core.schemas import ActionRequest


def test_action_request_has_required_contract_fields():
    request = ActionRequest(
        action_type="FILE_READ",
        target_system="local_file",
        target="sample.txt",
    )

    assert request.schema_version == "0.1"
    assert request.run_id.startswith("run_")
    assert request.action_id.startswith("act_")
