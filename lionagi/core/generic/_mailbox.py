from collections import deque
from pydantic import Field, field_validator
from .abc import BaseComponent
from .pile import Pile
from .mail import Mail
from ._sequence import CategorizedSequence


class MailBox(BaseComponent):
    
    pile: Pile = Field(
        default_factory=Pile,
        title="Pile",
        description="The pile of mails in the mailbox.",
    )
    
    sequence_in: CategorizedSequence = Field(
        default_factory=CategorizedSequence,
        title="Sequence in",
        description="The sequence of incoming mails in the mailbox. categorised by sender.",
    )
    
    sequence_out: deque = Field(
        default_factory=deque,
        title="Sequence out",
        description="The sequence of outgoing mails in the mailbox.",
    )
    
    
    def append(self, in_=False, out_=False, mail: Mail=None):
        if sum([1 if i else 0 for i in [in_, out_]]) != 1:
            raise ValueError("One and only one of in_ or out_ must be True.")
        
        if not isinstance(mail, Mail):
            raise ValueError("Mail must be a Mail instance.")

        self.pile.append(mail)
        if in_:
            self.sequence_in.append(mail.sender, mail)
        else:
            self.sequence_out.append(mail)


    def popleft(self, out_=False, in_=False, sender=None) -> Mail:
        if sum([1 if i else 0 for i in [in_, out_]]) != 1:
            raise ValueError("One and only one of in_ or out_ must be True.")
        
        mail = None
        if in_:
            if not sender:
                raise ValueError("Sender must be provided when popping an incoming mail.")
            mail = self.sequence_in.popleft(sender)
            
        if out_:
            try:
                mail = self.sequence_out.popleft()
            except IndexError:
                    return None
        
        if mail:
            return self.pile.pop(mail)
        return None
        
        
    def __str__(self) -> str:
        """
        Returns a string representation of the MailBox instance.

        Returns:
            str: A string describing the number of pending incoming and
                outgoing mails in the MailBox.
        """
        return (
            f"MailBox with {len(self.sequence_in)} pending incoming mails and "
            f"{len(self.sequence_out)} pending outgoing mails."
        )
