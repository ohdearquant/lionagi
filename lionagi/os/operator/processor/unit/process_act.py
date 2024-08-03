from typing import Literal
from lion_core.libs import validate_mapping
from lionagi.os.primitives import ActionRequest, ActionResponse, note, Form
from lionagi.os.session.branch.branch import Branch
from lionagi.os.operator.processor.unit.process_chat import process_action_request


async def process_act(
    branch: Branch,
    form: Form,
    actions: dict,
    handle_unmatched: Literal["ignore", "raise", "remove", "force"] = "force",
    return_branch: bool = False,
) -> Form | tuple[Branch, Form]:
    """
    Process actions for a given branch and form.

    Args:
        branch: The branch to process actions for.
        form: The form associated with the actions.
        actions: A dictionary of actions to process.
        handle_unmatched: Strategy for handling unmatched actions.
        return_branch: Whether to return the branch along with the form.

    Returns:
        The updated form, or a tuple of (branch, form) if return_branch is True.

    Raises:
        ValueError: If no requests are found when processing actions.
    """
    if "actioin_performed" in form.all_fields and form.action_performed:
        return form

    action_keys = [f"action_{i+1}" for i in range(len(actions))]
    validated_actions = note(
        **validate_mapping(
            actions,
            action_keys,
            handle_unmatched=handle_unmatched,
        )
    )

    requests = []
    for k in action_keys:
        _func = validated_actions.get([k, "function"], "").replace("functions.", "")
        msg = ActionRequest(
            function=_func,
            arguments=validated_actions[k, "arguments"],
            sender=branch.ln_id,
            recipient=branch.tool_manager.registry[_func].ln_id,
        )
        requests.append(msg)
        branch.add_message(action_request=msg)

    if requests:
        out = await process_action_request(
            branch=branch, _msg=None, invoke_tool=True, action_request=requests
        )

        if out is False:
            raise ValueError("No requests found.")

        len_actions = len(validated_actions)
        action_responses = [
            i for i in branch.messages[-len_actions:] if isinstance(i, ActionResponse)
        ]

        _res = note()
        for idx, item in enumerate(action_responses):
            _res.insert(["action", idx], item.to_dict())

        len1 = len(form.action_response)

        if getattr(form, "action_response", None) is None:
            acts_ = _res.get(["action"], [])
            form.append_to_request("action_response", [i for i in acts_])
        else:
            form.action_response.extend([i for i in _res.get(["action"], [])])

        if len(form.action_response) > len1:
            form.append_to_request("action_performed", True)

    return (branch, form) if return_branch else form
