from collections import deque
from enum import Enum
from lionagi.libs import AsyncUtil
from lionagi.core.generic import Node



class MailCategory(str, Enum):
    MESSAGES = "messages"
    TOOL = "tool"
    SERVICE = "service"
    MODEL = "model"
    NODE = "node"
    NODE_LIST = "node_list"
    NODE_ID = "node_id"
    START = "start"
    END = "end"
    CONDITION = "condition"


class BaseMail:

    def __init__(self, sender_id, recipient_id, category, package):
        self.sender_id = sender_id
        self.recipient_id = recipient_id
        try:
            if isinstance(category, str):
                category = MailCategory(category)
            if isinstance(category, MailCategory):
                self.category = category
            else:
                raise ValueError(
                    f"Invalid request title. Valid titles are" f" {list(MailCategory)}"
                )
        except Exception as e:
            raise ValueError(
                f"Invalid request title. Valid titles are "
                f"{list(MailCategory)}, Error: {e}"
            ) from e
        self.package = package


class StartMail(Node):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending_outs = deque()

    def trigger(self, context, structure_id, executable_id):
        start_mail_content = {"context": context, "structure_id": structure_id}
        start_mail = BaseMail(
            sender_id=self.id_,
            recipient_id=executable_id,
            category="start",
            package=start_mail_content,
        )
        self.pending_outs.append(start_mail)


class MailTransfer(Node):
    def __init__(self):
        super().__init__()
        self.pending_ins = {}
        self.pending_outs = deque()
        
        


class MailManager:
    """
    Manages the sending, receiving, and storage of mail items between various sources.

    This class acts as a central hub for managing mail transactions within a system. It allows for the addition
    and deletion of sources, and it handles the collection and dispatch of mails to and from these sources.

    Attributes:
            sources (Dict[str, Any]): A dictionary mapping source identifiers to their attributes.
            mails (Dict[str, Dict[str, deque]]): A nested dictionary storing queued mail items, organized by recipient
                    and sender.
    """

    def __init__(self, sources=None):
        self.sources = {}
        self.mails = {}
        if sources:
            self.add_sources(sources)
        self.execute_stop = False

    def add_sources(self, sources):
        if isinstance(sources, dict):
            for _, v in sources.items():
                if v.id_ not in self.sources:
                    self.sources[v.id_] = v
                    self.mails[v.id_] = {}
        elif isinstance(sources, list):
            for v in sources:
                if v.id_ not in self.sources:
                    self.sources[v.id_] = v
                    self.mails[v.id_] = {}
        else:
            raise ValueError("Failed to add source, please input list or dict.")

    @staticmethod
    def create_mail(sender_id, recipient_id, category, package):
        return BaseMail(sender_id, recipient_id, category, package)

    # def add_source(self, sources: list[Node]):
    #     for source in sources:
    #         if source.id_ in self.sources:
    #             # raise ValueError(f"Source {source.id_} exists, please input a different name.")
    #             continue
    #         self.sources[source.id_] = source
    #         self.mails[source.id_] = {}

    def delete_source(self, source_id):
        if source_id not in self.sources:
            raise ValueError(f"Source {source_id} does not exist.")
        # if self.mails[source_id]:
        #     raise ValueError(f"None empty pending mails in source {source_id}")
        self.sources.pop(source_id)
        self.mails.pop(source_id)

    def collect(self, sender_id):
        if sender_id not in self.sources:
            raise ValueError(f"Sender source {sender_id} does not exist.")
        while self.sources[sender_id].pending_outs:
            mail_ = self.sources[sender_id].pending_outs.popleft()
            if mail_.recipient_id not in self.sources:
                raise ValueError(
                    f"Recipient source {mail_.recipient_id} does not exist"
                )
            if mail_.sender_id not in self.mails[mail_.recipient_id]:
                self.mails[mail_.recipient_id].update({mail_.sender_id: deque()})
            self.mails[mail_.recipient_id][mail_.sender_id].append(mail_)

    def send(self, recipient_id):
        if recipient_id not in self.sources:
            raise ValueError(f"Recipient source {recipient_id} does not exist.")
        if not self.mails[recipient_id]:
            return
        for key in list(self.mails[recipient_id].keys()):
            mails_deque = self.mails[recipient_id].pop(key)
            if key not in self.sources[recipient_id].pending_ins:
                self.sources[recipient_id].pending_ins[key] = mails_deque
            else:
                while mails_deque:
                    mail_ = mails_deque.popleft()
                    self.sources[recipient_id].pending_ins[key].append(mail_)

    def collect_all(self):
        for ids in self.sources:
            self.collect(ids)

    def send_all(self):
        for ids in self.sources:
            self.send(ids)

    async def execute(self, refresh_time=1):
        while not self.execute_stop:
            self.collect_all()
            self.send_all()
            await AsyncUtil.sleep(refresh_time)