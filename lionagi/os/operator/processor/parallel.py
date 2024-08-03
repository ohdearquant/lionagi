"""
Copyright 2024 HaiyangLi

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from lionagi.libs.ln_func_call import rcall, pcall
from lionagi.libs import convert, AsyncUtil

from lionagi.core.collections.abc import Directive
from lionagi.core.collections import iModel
from lionagi.core.validator.validator import Validator
from lionagi.core.session.branch import Branch
from lionagi.core.unit.util import retry_kwargs


class ParallelUnit(Directive):

    default_template = None

    def __init__(
        self, session, imodel: iModel = None, template=None, rulebook=None
    ) -> None:

        self.branch = session
        if imodel and isinstance(imodel, iModel):
            session.imodel = imodel
            self.imodel = imodel
        else:
            self.imodel = session.imodel
        self.form_template = template or self.default_template
        self.validator = Validator(rulebook=rulebook) if rulebook else Validator()

    async def pchat(self, *args, **kwargs):

        kwargs = {**retry_kwargs, **kwargs}
        return await rcall(self._parallel_chat, *args, **kwargs)

    async def _parallel_chat(
        self,
        instruction: str,
        num_instances=1,
        context=None,
        sender=None,
        messages=None,
        tools=False,
        out=True,
        invoke: bool = True,
        requested_fields=None,
        persist_path=None,
        branch_config={},
        explode=False,
        include_mapping=True,
        default_key="response",
        **kwargs,
    ):

        branches = {}

        async def _inner(i, ins_, cxt_):

            branch_ = Branch(
                messages=messages,
                service=self.session.default_branch.service,
                llmconfig=self.session.default_branch.llmconfig,
                persist_path=persist_path,
                **branch_config,
            )

            branch_.branch_name = branch_.id_

            if tools:
                branch_.tool_manager = self.session.default_branch.tool_manager

            res_ = await branch_.chat(
                instruction=ins_ or instruction,
                context=cxt_ or context,
                sender=sender,
                tools=tools,
                invoke=invoke,
                out=out,
                requested_fields=requested_fields,
                **kwargs,
            )

            branches[branch_.id_] = branch_
            if include_mapping:
                return {
                    "instruction": ins_ or instruction,
                    "context": cxt_ or context,
                    "branch_id": branch_.id_,
                    default_key: res_,
                }

            else:
                return res_

        async def _inner_2(i, ins_=None, cxt_=None):
            """returns num_instances of branches performing for same task/context"""
            tasks = [_inner(i, ins_, cxt_) for _ in range(num_instances)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_3(i):
            """different instructions but same context"""
            tasks = [_inner_2(i, ins_=ins_) for ins_ in convert.to_list(instruction)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_3_b(i):
            """different context but same instruction"""
            tasks = [_inner_2(i, cxt_=cxt_) for cxt_ in convert.to_list(context)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_4(i):
            """different instructions and different context"""

            tasks = []
            if explode:
                tasks = [
                    _inner_2(i, ins_=ins_, cxt_=cxt_)
                    for ins_ in convert.to_list(instruction)
                    for cxt_ in convert.to_list(context)
                ]
            else:
                tasks = [
                    _inner_2(i, ins_=ins_, cxt_=cxt_)
                    for ins_, cxt_ in zip(
                        convert.to_list(instruction), convert.to_list(context)
                    )
                ]

            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        if len(convert.to_list(instruction)) == 1:
            if len(convert.to_list(context)) == 1:
                out_ = await _inner_2(0)
                self.session.branches.update(branches)
                return out_

            elif len(convert.to_list(context)) > 1:
                out_ = await _inner_3_b(0)
                self.session.branches.update(branches)
                return out_

        elif len(convert.to_list(instruction)) > 1:
            if len(convert.to_list(context)) == 1:
                out_ = await _inner_3(0)
                self.session.branches.update(branches)
                return out_

            elif len(convert.to_list(context)) > 1:
                out_ = await _inner_4(0)
                self.session.branches.update(branches)
                return out_
