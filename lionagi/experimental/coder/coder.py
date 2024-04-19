from pathlib import Path
from lionagi.libs import SysUtil, ParseUtil
from pydantic import Field
from lionagi.core import Session
from lionagi.core.form.action_form import ActionForm
from experimental.coder.prompts import coder_prompts


from os import getenv
from pathlib import Path

E2B_key_scheme = "E2B_API_KEY"


def save_to_file(
    text,
    directory: Path | str,
    filename: str,
    timestamp: bool = True,
    dir_exist_ok: bool = True,
    time_prefix: bool = False,
    custom_timestamp_format: str | None = None,
    random_hash_digits=0,
    verbose=True,
):
    file_path = SysUtil.create_path(
        directory=directory,
        filename=filename,
        timestamp=timestamp,
        dir_exist_ok=dir_exist_ok,
        time_prefix=time_prefix,
        custom_timestamp_format=custom_timestamp_format,
        random_hash_digits=random_hash_digits,
    )

    with open(file_path, "w") as file:
        file.write(text)

    if verbose:
        print(f"Text saved to: {file_path}")

    return True


class CodeForm(ActionForm):
    template_name: str = Field("code form template")
    language: str = Field("python")
    guidance_response: str = Field(coder_prompts["guidance_response"])
    # rename signature to assignments in form
    assignments: str = (
        "sentence, language, guidance_response -> reason, action_needed, actions, answer"
    )
    code: str | None = Field(None)

    def __init__(
        self,
        sentence=None,
        instruction=None,
        confidence_score=False,
        task=None,
        **kwargs,
    ):
        super().__init__(
            sentence=sentence,
            instruction=instruction,
            confidence_score=confidence_score,
            **kwargs,
        )
        if task:
            self.task = task or coder_prompts.get("write_code")
        if confidence_score:
            self.output_fields.append("confidence_score")


class Coder:

    review_prompt = "Please review the following code and remove any unnecessary markdown or descriptions:\n\n{code}\n"

    def __init__(self, **kwargs) -> None:
        self.interpreter = None
        self._set_up_interpreter()
        self.form_template = CodeForm
        self.session = Session(**kwargs)
        self.forms = []
        self.verbose = True
        self.persist_dir = Path.cwd() / "code_files"
        pass

    async def write_codes(self, **kwargs):
        form = self.form_template(**kwargs)
        form = await self.session.chat(form=form)
        form.code = ParseUtil.extract_code_block(form.answer, language=form.language)
        self.forms.append(form)
        return form

    async def review_codes(self, form, **kwargs):
        instruction = coder_prompts["review_code"].format(code=form.code)
        return await self.write_codes(instruction=instruction, **kwargs)

    def execute_codes(self, form, **kwargs):
        with self.interpreter as sandbox:
            execution = sandbox.notebook.exec_cell(form.code, **kwargs)

            if self.verbose:
                print(f"Execution Output:\n{execution.text}")

            if execution.error:
                setattr(form, "execution_error", execution.error)
                setattr(form, "execution", None)

            elif execution.text:
                self.save_code_file(form)
                setattr(form, "execution", execution.text)
                setattr(form, "execution_error", None)

        return form

    def save_code_file(self, form, **kwargs):

        default_kwargs = {
            "file_name": "code.py",
            "timestamp": False,
            "random_hash_digits": 3,
            "verbose": True,
            "directory": self.persist_dir,
        }

        kwargs = {**default_kwargs, **kwargs}
        return save_to_file(text=form.code, **kwargs)

    def read_code_file(self, file_path):
        with open(file_path, "r") as file:
            return file.read()

    def _set_up_interpreter(self, key_scheme=E2B_key_scheme):
        SysUtil.check_import("e2b_code_interpreter")
        from e2b_code_interpreter import CodeInterpreter

        self.interpreter = CodeInterpreter(getenv(key_scheme))
