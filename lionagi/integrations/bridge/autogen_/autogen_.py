from typing import Dict, Union


def get_ipython_user_proxy():

    try:
        from lionagi.libs import SysUtil

        SysUtil.check_import("autogen", pip_name="pyautogen")

        import autogen
        from IPython import get_ipython
    except Exception as e:
        raise ImportError(f"Please install autogen and IPython. {e}")

    class IPythonUserProxyAgent(autogen.UserProxyAgent):

        def __init__(self, name: str, **kwargs):
            super().__init__(name, **kwargs)
            self._ipython = get_ipython()

        def generate_init_message(self, *args, **kwargs) -> Union[str, Dict]:
            return (
                super().generate_init_message(*args, **kwargs)
                + """If you suggest code, the code will be executed in IPython."""
            )

        def run_code(self, code, **kwargs):
            result = self._ipython.run_cell("%%capture --no-display cap\n" + code)
            log = self._ipython.ev("cap.stdout")
            log += self._ipython.ev("cap.stderr")
            if result.result is not None:
                log += str(result.result)
            exitcode = 0 if result.success else 1
            if result.error_before_exec is not None:
                log += f"\n{result.error_before_exec}"
                exitcode = 1
            if result.error_in_exec is not None:
                log += f"\n{result.error_in_exec}"
                exitcode = 1
            return exitcode, log, None

    return IPythonUserProxyAgent


def get_autogen_coder(
    llm_config=None,
    code_execution_config=None,
    kernal="python",
    config_list=None,
    max_consecutive_auto_reply=15,
    temperature=0,
    cache_seed=42,
    env_="local",
    assistant_instruction=None,
):
    assistant = ""
    try:
        from lionagi.libs import SysUtil

        SysUtil.check_import("autogen", pip_name="pyautogen")

        import autogen
        from autogen.agentchat.contrib.gpt_assistant_agent import GPTAssistantAgent
    except Exception as e:
        raise ImportError(f"Please install autogen. {e}")

    if env_ == "local":
        assistant = autogen.AssistantAgent(
            name="assistant",
            llm_config=llm_config
            or {
                "cache_seed": cache_seed,
                "config_list": config_list,
                "temperature": temperature,
            },
        )

    elif env_ == "oai_assistant":
        assistant = GPTAssistantAgent(
            name="Coder Assistant",
            llm_config={
                "tools": [{"type": "code_interpreter"}],
                "config_list": config_list,
            },
            instructions=assistant_instruction,
        )

    if kernal == "python":
        user_proxy = autogen.UserProxyAgent(
            name="user_proxy",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=max_consecutive_auto_reply,
            is_termination_msg=lambda x: x.get("content", "")
            .rstrip()
            .endswith("TERMINATE"),
            code_execution_config=code_execution_config
            or {
                "work_dir": "coding",
                "use_docker": False,
            },
        )
        return user_proxy, assistant

    elif kernal == "ipython":
        user_proxy = get_ipython_user_proxy(
            "ipython_user_proxy",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=max_consecutive_auto_reply,
            is_termination_msg=lambda x: x.get("content", "")
            .rstrip()
            .endswith("TERMINATE")
            or x.get("content", "").rstrip().endswith('"TERMINATE".'),
        )
        return user_proxy, assistant

    # # Sample Usage Pattern
    # context = "def my_function():\n    pass\n"
    # task1 = "I need help with the following code:\n"
    # task2 = "Please write a function that returns the sum of two numbers."

    # user_proxy, assistant = get_autogen_coder()
    # user_proxy.initiate_chat(assistant, message=task1+context)
    # user_proxy.send(recipient=assistant, message=task2)
