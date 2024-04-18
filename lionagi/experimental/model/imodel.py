from ..configs import oai_schema


class BaseIntelligentModel:

    def __init__(
        self, service=None, default_config=oai_schema["chat"]["config"], **kwargs
    ) -> None:
        # kwargs are the individual parameters that the model can take
        # different for different models
        self.service = (service,)
        self.config = ({**default_config, **kwargs},)

    async def __call__(
        self, payload, service=None, endpoint_="chat/completions", method="post"
    ):
        service = service or self.service
        return await service.serve(payload=payload, endpoint_=endpoint_, method=method)

    def set_service(self, service):
        self.service = service

    def set_config(self, config):
        self.config = config

    def change_model(self, model):
        self.config["model"] = model

    def change_temperature(self, temperature):
        self.config["temperature"] = temperature

    def revert_to_default_config(self):
        self.config = oai_schema["chat"]["config"]

    def modify_config(self, **kwargs):
        self.config = {**self.config, **kwargs}


class IModel(BaseIntelligentModel):

    def __init__(self, service=None, default_model_kwargs=None, **kwargs) -> None:
        super().__init__(service, default_model_kwargs, **kwargs)
