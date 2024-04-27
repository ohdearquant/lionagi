import asyncio
from typing import Callable, Dict, Any, Optional, Union
from lionagi.core.session.branch import Branch


class Agent:
    def __init__(self, name: Optional[str] = None):
        self.name = name
        self.context = {}
        self.pre_middleware_stack = []
        self.post_middleware_stack = []

    def use_pre_middleware(self, middleware: Callable):
        """
        Register a middleware to be applied before processing the instruction.
        """
        self.pre_middleware_stack.append(middleware)

    def use_post_middleware(self, middleware: Callable):
        """
        Register a middleware to be applied after generating a assistant_response.
        """
        self.post_middleware_stack.append(middleware)

    async def apply_middleware(self, data, middleware_stack):
        """
        Apply a stack of middleware to data, supporting asynchronous operations.
        """
        for middleware in middleware_stack:
            if asyncio.iscoroutinefunction(middleware):
                data = await middleware(data, self.context)
            else:
                data = middleware(data, self.context)
        return data

    def update_context(self, new_context: Dict[str, Any]):
        self.context.update(new_context)

    def reset_context(self):
        self.context = {}

    async def process_instruction(self, instruction: Any) -> Any:
        raise NotImplementedError("Subclasses must implement this method.")

    async def generate_response(self) -> Any:
        raise NotImplementedError("Subclasses must implement this method.")

    async def receive_instruction(self, instruction: Any):
        instruction = await self.apply_middleware(
            instruction, self.pre_middleware_stack
        )
        response = await self.process_instruction(instruction)
        response = await self.apply_middleware(response, self.post_middleware_stack)
        return response


class ConversationalAgent(Agent):
    def __init__(self, name: Optional[str] = None):
        super().__init__(name)
        self.conversation_history = []
        self.middleware_stack = []

    def use_middleware(self, middleware: Callable):
        """
        Register middleware to be applied to conversational events, including instructions and responses.
        """
        self.middleware_stack.append(middleware)

    async def apply_middleware(self, event, event_type: str):
        """
        Apply middleware to conversational events, supporting both sync and async middleware.
        """
        for middleware in self.middleware_stack:
            if asyncio.iscoroutinefunction(middleware):
                event = await middleware(event, event_type, self.context)
            else:
                event = middleware(event, event_type, self.context)
        return event

    async def process_instruction(self, instruction: Any) -> Any:
        # Implement specific logic for processing instructions.
        return "Processed instruction assistant_response."

    async def generate_response(self, processed_instruction: Any) -> Any:
        # Implement logic to generate a assistant_response based on the processed instruction.
        return "Generated conversational assistant_response."

    async def receive_instruction(self, instruction: Any):
        """
        Entry point for receiving and processing instructions within a conversational context.
        """
        instruction = await self.apply_middleware(instruction, "instruction")
        processed_instruction = await self.process_instruction(instruction)
        response = await self.generate_response(processed_instruction)
        response = await self.apply_middleware(response, "assistant_response")

        # Abstracted conversation history management.
        self.conversation_history.append({"type": "instruction", "data": instruction})
        self.conversation_history.append(
            {"type": "assistant_response", "data": response}
        )

        return response


class TaskAgent(ConversationalAgent):
    def __init__(
        self,
        name: Optional[str] = None,
        task_handlers: Optional[Dict[str, Callable]] = None,
        fallback_handler: Optional[Callable] = None,
    ):
        super().__init__(name)
        self.task_handlers = task_handlers or {}
        self.fallback_handler = fallback_handler

    async def identify_task_and_args(self, instruction: Any) -> Union[str, dict]:
        """
        Dynamically identify the task and extract arguments from the instruction.
        Override this method to implement specific task identification logic.
        """
        # Placeholder for demonstration. Implement dynamic task identification and argument extraction.
        return "task_name", {}

    async def handle_task(self, task_name: str, **kwargs) -> Any:
        """
        Execute the task using the registered handler, with fallback if the task is unknown.
        """
        handler = self.task_handlers.get(task_name, self.fallback_handler)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    return await handler(**kwargs)
                else:
                    return handler(**kwargs)
            except Exception as e:
                print(f"Error executing task {task_name}: {e}")
                return "An error occurred while executing the task."
        else:
            return "Task handler not found."

    async def process_instruction(self, instruction: Any) -> Any:
        """
        Process instructions specific to task execution, using dynamic task identification.
        """
        task_name, task_args = await self.identify_task_and_args(instruction)
        response = await self.handle_task(task_name, **task_args)
        return response


class LearningAgent(TaskAgent):
    def __init__(
        self, name: Optional[str] = None, learning_model: Optional[Any] = None
    ):
        super().__init__(name)
        self.learning_model = learning_model

    async def learn_from_interaction(self, interaction_data: Any):
        """
        Update the learning model based on interaction data.
        This method should be implemented to integrate specific learning algorithms.
        """
        if self.learning_model:
            # Placeholder for learning logic. Implement learning model updates based on interaction data.
            print("Learning from interaction...")

    async def adapt_response_strategy(self, instruction: Any) -> Any:
        """
        Adapt the assistant_response strategy based on what has been learned.
        Override this method to implement adaptive assistant_response strategies.
        """
        # Example implementation. Use the learning model to adapt the assistant_response strategy.
        print("Adapting assistant_response strategy based on learned patterns...")
        # Placeholder for demonstration. Implement adaptive assistant_response generation based on the learning model.
        return "Adapted assistant_response based on learning."

    async def process_instruction(self, instruction: Any) -> Any:
        """
        Process instruction with learning and adaptation.
        """
        # Incorporate learning into the instruction processing.
        response = await super().process_instruction(instruction)
        await self.learn_from_interaction(instruction)
        adapted_response = await self.adapt_response_strategy(instruction)
        return adapted_response


from abc import ABC, abstractmethod
from typing import Any, Dict


class AgentInterface(ABC):
    @abstractmethod
    async def receive_instruction(
        self, instruction: Dict[str, Any], context: Dict[str, Any]
    ) -> None:
        """
        Process a received instruction along with its context.
        """
        pass

    @abstractmethod
    async def generate_response(self) -> Dict[str, Any]:
        """
        Generate a assistant_response based on the current state and context.
        """
        pass


class ServiceAccessLayer:
    def __init__(self, branch_reference: "Branch"):
        self.branch_reference = branch_reference

    async def use_tool(self, tool_name: str, *args, **kwargs) -> Any:
        """
        Access and use a tools registered within the referenced branch.
        """
        tool = self.branch_reference.tool_manager.get_tool(tool_name)
        return await tool.execute(*args, **kwargs)

    async def call_service(self, service_name: str, *args, **kwargs) -> Any:
        """
        Call a provider provided by the referenced branch.
        """
        service = self.branch_reference.get_service(service_name)
        return await service(*args, **kwargs)


# class ConversationContextManager:
#     def __init__(self, conversation_storage: "ConversationStorage"):
#         self.conversation_storage = conversation_storage

#     def get_conversation_history(self, conversation_id: str) -> Dict[str, Any]:
#         """
#         Retrieve the conversation history given a conversation ID.
#         """
#         return self.conversation_storage.get_history(conversation_id)

#     def update_conversation_context(
#             self, conversation_id: str, update: Dict[str, Any]
#     ) -> None:
#         """
#         Update the conversation context for a specific conversation ID.
#         """
#         self.conversation_storage.update_context(conversation_id, update)


# class LearningFramework:
#     def __init__(self, learning_model: Any, learning_service: Any):
#         self.learning_model = learning_model
#         self.learning_service = learning_service

#     async def learn_from_interaction(self, interaction_data: Dict[str, Any]) -> None:
#         """
#         Update the learning model based on interaction data.
#         """
#         # Update the embedded model
#         self.learning_model.update(interaction_data)

#         # Optionally send data to an external learning provider
#         await self.learning_service.send_data_for_learning(interaction_data)

#     async def adapt_response_strategy(self, context: Dict[str, Any]) -> Dict[str, Any]:
#         """
#         Adapt the assistant_response strategy based on learned patterns and context.
#         """
#         # Use the learning model to determine the adaptation strategy
#         adaptation_strategy = self.learning_model.determine_strategy(context)
#         return adaptation_strategy
