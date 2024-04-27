from typing import Type, Callable
from lionagi.libs import BaseService
from lionagi.integrations.config.oai_configs import oai_chat_schema


class Model:

    def __init__(
        self, 
        model: str = None, 
        service: BaseService | Type[BaseService]  = None,
        service_kwargs: dict ={},
        endpoint_schema: dict = oai_chat_schema,
        **kwargs
    ):
        self.allowed_params = endpoint_schema["required"] + endpoint_schema["optional"]
        self.service = self._set_up_service(service, service_kwargs)
        self.config = self._set_up_params(self.allowed_params, endpoint_schema["config"], **kwargs)
        if model:
            self.config['model'] = model
        self.model_name = self.config['model']

    def _set_up_params(self, allowed_params=[], default_config={}, **kwargs):
        params = {**default_config, **kwargs}
        
        if allowed_params != []:
            if len(not_allowed := [k for k in params.keys() if k not in allowed_params]) > 0:
                raise ValueError(f'Not allowed parameters: {not_allowed}')
        
        return params

    def _set_up_service(self, service=None, service_kwargs=None):
        if service is None:
            try:
                from lionagi.integrations.provider import Services
                return Services.OpenAI(**service_kwargs)
            except:
                raise ValueError("No available service")
        else:
            from lionagi.libs import BaseService
            if issubclass(service, BaseService):
                return service
            elif isinstance(service, (Callable, Type[BaseService])):
                return service(**service_kwargs)
            raise ValueError("Invalid model provider service")