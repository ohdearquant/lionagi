import os
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
from googleapiclient.errors import HttpError
from email.message import EmailMessage

from .auth import GoogleAPI

scope = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailClient(GoogleAPI):

    def __init__(self, user_id=None):
        super().__init__("gmail", "v1", scope, user_id=user_id)

    def create_email(
        self,
        message=None,
        to=None,
        from_email=None,
        subject=None,
        body=None,
        filename=None,
    ):
        if not filename:
            message = message or self._create_email(
                to=to, subject=subject, body=body, from_email=from_email or self.user_id
            )
            return message
        if filename:
            message = self.create_email_message_with_attachment(
                to=to,
                subject=subject,
                body=body,
                from_email=from_email,
                file_path=filename,
            )
            return message

    def send_email(self, message=None, **kwargs):
        try:
            message = message or self.create_email(**kwargs)
            send_message = (
                self.service.users()
                .messages()
                .send(userId="me", body=message)
                .execute()
            )

            return send_message
        except HttpError as error:
            print(f"An error occurred: {error}")
            return None

    def create_draft(self, message=None, **kwargs):
        message = message or self.create_email(**kwargs)
        draft = (
            self.service.users()
            .drafts()
            .create(userId="me", body={"message": message})
            .execute()
        )
        return draft

    def get_draft(self, draft_id=None):
        if draft_id:
            draft = (
                self.service.users().drafts().get(userId="me", id=draft_id).execute()
            )
            return draft
        drafts = self.service.users().drafts().list(userId="me").execute()
        return drafts.get("drafts", [])

    def update_draft(self, draft_id, **kwargs):
        message_body = self.create_email(**kwargs)
        updated_draft = (
            self.service.users()
            .drafts()
            .update(userId="me", id=draft_id, body={"message": message_body})
            .execute()
        )
        return updated_draft

    def delete_draft(self, draft_id):
        self.service.users().drafts().delete(userId="me", id=draft_id).execute()

    def get_message_data(self, message_id):
        """Retrieve email data using the message ID."""
        message_data = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        # Parsing and extracting the message data
        return message_data

    def search_messages(self, query="", max_results=50):
        """Search for email messages using a query."""
        response = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = response.get("messages", [])
        return [self.get_message_data(msg["id"]) for msg in messages]

    def _create_email(self, to, subject, body, from_email=None):
        message = EmailMessage()
        message.set_content(body)
        message["To"] = to
        message["From"] = from_email or self.user_id
        message["Subject"] = subject
        return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}

    def add_attachment(self, message, filename):
        ctype, encoding = mimetypes.guess_type(filename)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"

        maintype, subtype = ctype.split("/", 1)
        with open(filename, "rb") as fp:
            msg = MIMEBase(maintype, subtype)
            msg.set_payload(fp.read())
            encoders.encode_base64(msg)

        msg.add_header(
            "Content-Disposition", "attachment", filename=os.path.basename(filename)
        )
        message.attach(msg)

    def create_email_message_with_attachment(
        self, to, subject, body, from_email, file_path
    ):
        message = MIMEMultipart()
        message["To"] = to
        message["From"] = from_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        self.add_attachment(message, file_path)

        return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
